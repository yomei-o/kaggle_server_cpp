"""kbridge の「操作」層 — Jupyter クライアントの上に /sh, /gpu, /job を組み立てる。

Kaggle 側の実体は kaggle/kbagent.py 1 ファイル。ここはそれを

  * 必要なら（未配置 or 版ズレ）アップロードし、
  * `import kbagent; print(json.dumps(kbagent.xxx(...)))` を実行し、
  * 標準出力の JSON を拾う

だけの薄い層。C++ 版 cpp/pure/ops.hpp も同じ手順・同じ生成コードを使う
（生成するコード文字列が両実装で一致していることがパリティの肝）。
"""

import json
import os

from .jupyter import JupyterError

AGENT_VERSION = "0.1.0"
AGENT_DIR = "/kaggle/working/.kbridge"
AGENT_REMOTE_PATH = ".kbridge/kbagent.py"   # contents API から見た相対パス
EXIT_MARKER = "__KBRIDGE_EXIT__"

_PRELUDE = (
    "import sys, json\n"
    "sys.path.insert(0, %r) if %r not in sys.path else None\n" % (AGENT_DIR, AGENT_DIR)
)


def agent_source():
    """同梱している kaggle/kbagent.py の中身を返す。"""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.normpath(os.path.join(here, "..", "..", "kaggle", "kbagent.py"))
    with open(path, "rb") as f:
        return f.read()


def ensure_agent(client, force=False):
    """Kaggle 側に kbagent.py が正しい版で置かれている状態にする。

    毎回アップロードすると往復が増えるので、まず版を訊いて、違うときだけ送る。
    """
    if not force:
        code = _PRELUDE + (
            "v = ''\n"
            "try:\n"
            "    import kbagent\n"
            "    v = getattr(kbagent, 'VERSION', '')\n"
            "except Exception:\n"
            "    pass\n"
            "print(json.dumps({'version': v}))\n"
        )
        try:
            if client.execute_json(code, timeout=60).get("version") == AGENT_VERSION:
                return False
        except JupyterError:
            pass

    client.put_file(AGENT_REMOTE_PATH, agent_source())
    code = _PRELUDE + (
        "import importlib\n"
        "import kbagent\n"
        "importlib.reload(kbagent)\n"
        "print(json.dumps({'version': kbagent.VERSION}))\n"
    )
    got = client.execute_json(code, timeout=60).get("version")
    if got != AGENT_VERSION:
        raise JupyterError("kbagent version mismatch after upload: %r" % (got,))
    return True


def _call(client, expr, timeout=120.0):
    """kbagent の関数を呼び、返り値の dict を受け取る。"""
    ensure_agent(client)
    code = _PRELUDE + "import kbagent\nprint(json.dumps(kbagent.%s))\n" % expr
    return client.execute_json(code, timeout=timeout)


# ------------------------------------------------------------------ /sh

def _sh_code(cmd, cwd, timeout):
    ensure_arg = "None" if timeout is None else repr(float(timeout))
    return _PRELUDE + ("import kbagent\nkbagent.sh(%r, cwd=%r, timeout=%s)\n"
                       % (cmd, cwd, ensure_arg))


def _split_marker(text):
    """kbagent.sh が最後に出す __KBRIDGE_EXIT__ 行を本文から切り離す。"""
    idx = text.rfind(EXIT_MARKER)
    if idx < 0:
        return text, None
    head = text[:idx].rstrip("\n")
    tail = text[idx + len(EXIT_MARKER):].strip()
    try:
        return head, json.loads(tail.splitlines()[0]) if tail else None
    except (ValueError, IndexError):
        return head, None


def sh(client, cmd, cwd="/kaggle/working", timeout=300.0):
    ensure_agent(client)
    # カーネル側のタイムアウトは少し短くして、こちらが切る前に終了コードを書かせる
    inner = max(float(timeout) - 5.0, 1.0)
    r = client.execute(_sh_code(cmd, cwd, inner), timeout=timeout)
    body, marker = _split_marker(r["stdout"])
    r["stdout"] = body
    r["exit_code"] = (marker or {}).get("exit_code")
    if marker and marker.get("timeout"):
        r["status"] = "timeout"
        r["ok"] = False
    elif r["status"] == "ok" and r["exit_code"] not in (0, None):
        r["ok"] = False
        r["status"] = "error"
        r["evalue"] = "command exited with %s" % r["exit_code"]
    return r


class ShellStream:
    """/sh/stream 用。マーカー行だけを本文から取り除きながら流す。

    マーカーはチャンクの切れ目をまたぐことがあるので、末尾を持ち越して判定する。
    """

    def __init__(self, emit):
        self.emit = emit
        self.hold = ""
        self.marker = None
        self.done = False

    def feed(self, ev):
        if ev.get("t") != "out":
            self.emit(ev)
            return
        self.hold += ev["d"]
        if self.done:
            self._take_marker()
            return
        idx = self.hold.find(EXIT_MARKER)
        if idx >= 0:
            head, self.hold = self.hold[:idx], self.hold[idx:]
            self.done = True
            if head:
                self.emit({"t": "out", "stream": ev.get("stream", "stdout"), "d": head})
            self._take_marker()
            return
        # マーカーの断片が末尾に残っている可能性がある分だけ手元に残す
        keep = len(EXIT_MARKER) - 1
        if len(self.hold) > keep:
            out, self.hold = self.hold[:-keep], self.hold[-keep:]
            self.emit({"t": "out", "stream": ev.get("stream", "stdout"), "d": out})

    def _take_marker(self):
        tail = self.hold[len(EXIT_MARKER):]
        if "\n" in tail or tail.strip().endswith("}"):
            try:
                self.marker = json.loads(tail.strip().splitlines()[0])
            except (ValueError, IndexError):
                pass

    def flush(self):
        if not self.done and self.hold:
            self.emit({"t": "out", "stream": "stdout", "d": self.hold})
            self.hold = ""


# ------------------------------------------------------------------ /gpu

def gpu(client, timeout=120.0):
    return _call(client, "gpu()", timeout=timeout)


# ------------------------------------------------------------------ /job

def job_start(client, cmd=None, code=None, name="job", cwd="/kaggle/working",
              env=None, timeout=120.0):
    if (cmd is None) == (code is None):
        raise ValueError("give exactly one of cmd or code")
    expr = ("start(cmd=%r, code=%r, name=%r, cwd=%r, env=%r)"
            % (cmd, code, name, cwd, env))
    return _call(client, expr, timeout=timeout)


def job_list(client, timeout=120.0):
    return _call(client, "ls()", timeout=timeout)


def job_status(client, job_id, timeout=120.0):
    return _call(client, "status(%r)" % job_id, timeout=timeout)


def job_log(client, job_id, offset=0, max_bytes=65536, timeout=120.0):
    return _call(client, "log(%r, offset=%d, max_bytes=%d)"
                 % (job_id, int(offset), int(max_bytes)), timeout=timeout)


def job_kill(client, job_id, timeout=120.0):
    return _call(client, "kill(%r)" % job_id, timeout=timeout)


def job_rm(client, job_id, timeout=120.0):
    return _call(client, "rm(%r)" % job_id, timeout=timeout)
