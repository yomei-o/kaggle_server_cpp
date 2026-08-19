"""パリティ試験 — python 版と cpp 版のローカルサーバが同じ応答を返すことを縛る。

  python tests/parity.py

やっていること: 偽 Jupyter サーバを**2 つ**（実装ごとに 1 つ）立て、それぞれに
python 版 / cpp 版のサーバをつなぎ、**同じリクエスト列**を投げて応答を比べる。
実装ごとに別のカーネルを使うのは、`execution_count` などの状態が混ざらないようにするため。

比較の規則（spec/API.md 6.）:
  * `impl` は除く（それ以外は一致していなければならない）
  * 実行ごとに変わる値はキーの有無と型だけ見る:
    elapsed / uptime / kernel_id / session_id / pid / id / started / ended /
    execution_count / log / cwd / raw / base_url
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import fake_jupyter  # noqa: E402
from e2e import req, ndjson, wait_up  # noqa: E402

# 値そのものは一致しなくてよいが、キーの有無と型は一致していなければならないもの
VOLATILE = {"elapsed", "uptime", "kernel_id", "session_id", "pid", "id", "started",
            "ended", "execution_count", "log", "cwd", "raw", "base_url", "log_size",
            "next_offset", "offset", "traceback", "exit_code_of_job"}

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[ OK ]" if cond else "[DIFF]", name,
                       ("  -> " + str(detail)) if detail else ""))


def normalize(v, key=None):
    """比較用に値をならす。volatile なキーは型名だけに潰す。"""
    if key in VOLATILE:
        return "<%s>" % type(v).__name__
    if isinstance(v, dict):
        return {k: normalize(x, k) for k, x in sorted(v.items()) if k != "impl"}
    if isinstance(v, list):
        return [normalize(x, key) for x in v]
    if isinstance(v, float):
        return round(v, 3)
    return v


def diff(a, b, path=""):
    """最初に食い違った場所を 1 つ返す（None なら一致）。"""
    if type(a) is not type(b):
        return "%s: 型が違う %s vs %s" % (path or "/", type(a).__name__,
                                        type(b).__name__)
    if isinstance(a, dict):
        ka, kb = set(a), set(b)
        if ka != kb:
            return "%s: キーが違う python のみ=%s cpp のみ=%s" % (
                path or "/", sorted(ka - kb), sorted(kb - ka))
        for k in sorted(ka):
            d = diff(a[k], b[k], path + "/" + str(k))
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return "%s: 長さが違う %d vs %d" % (path or "/", len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            d = diff(x, y, "%s[%d]" % (path, i))
            if d:
                return d
        return None
    if a != b:
        return "%s: %r vs %r" % (path or "/", a, b)
    return None


class Impl:
    def __init__(self, name, base, url, root, proc, srv):
        self.name, self.base, self.url, self.root = name, base, url, root
        self.proc, self.srv = proc, srv


def both(fn):
    """2 実装に同じことをして (python の結果, cpp の結果) を返す。"""
    return fn(IMPLS[0]), fn(IMPLS[1])


def compare(name, fn):
    try:
        ra, rb = both(fn)
    except Exception as e:                                   # noqa: BLE001
        check(name, False, "例外: %s" % e)
        return
    d = diff(normalize(ra), normalize(rb))
    check(name, d is None, d or "")


IMPLS = []


def clean_env(workdir):
    """本物の Kaggle を掴まないようにした環境を作る。

    リポジトリ直下に .kbridge.json（実セッションの URL）が置いてあると、
    そこを cwd にした側だけが起動時に自動接続してしまい、/healthz の
    connected が食い違う。両実装とも「何も無い作業ディレクトリ」で起動させる。
    """
    env = dict(os.environ)
    env.pop("KAGGLE_JUPYTER_URL", None)
    env["HOME"] = workdir
    env["USERPROFILE"] = workdir
    return env


def start_impl(name, port, fake_port, exe, verbose):
    srv, url, root = fake_jupyter.serve(fake_port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    workdir = tempfile.mkdtemp(prefix="kbparity-")
    env = clean_env(workdir)
    if name == "python":
        env["PYTHONPATH"] = os.path.join(ROOT, "python")
        proc = subprocess.Popen(
            [sys.executable, "-m", "kbridge.server", "--port", str(port)],
            cwd=workdir, env=env,
            stdout=subprocess.DEVNULL if not verbose else None)
    else:
        path = exe if os.path.isabs(exe) else os.path.join(ROOT, exe)
        if not os.path.exists(path):
            raise SystemExit("no such exe: %s  "
                             "(sh cpp/build/gcc.sh cpp/kbridge_server.cpp "
                             "-o kbridge_server.exe)" % path)
        proc = subprocess.Popen(
            [path, "--port", str(port),
             "--agent", os.path.join(ROOT, "kaggle", "kbagent.py")],
            cwd=workdir, env=env,
            stdout=subprocess.DEVNULL if not verbose else None)
    if not wait_up(base):
        proc.terminate()
        raise SystemExit("%s server did not come up on %s" % (name, base))
    return Impl(name, base, url, root, proc, srv)


def run():
    compare("GET /healthz", lambda i: req(i.base, "GET", "/healthz")[1])
    compare("POST /session",
            lambda i: req(i.base, "POST", "/session", {"url": i.url})[1])
    compare("GET /session", lambda i: req(i.base, "GET", "/session")[1])

    compare("POST /exec",
            lambda i: req(i.base, "POST", "/exec",
                          {"code": "print('parity'); y = 7 * 6"})[1])
    compare("POST /exec (状態が続く)",
            lambda i: req(i.base, "POST", "/exec", {"code": "print(y)"})[1])
    compare("POST /exec (例外)",
            lambda i: req(i.base, "POST", "/exec",
                          {"code": "raise ValueError('x')"})[1])
    compare("POST /exec (stderr)",
            lambda i: req(i.base, "POST", "/exec",
                          {"code": "import sys; print('E', file=sys.stderr)"})[1])
    compare("POST /exec (引数不正)",
            lambda i: req(i.base, "POST", "/exec", {})[1])
    compare("POST /exec (timeout)",
            lambda i: req(i.base, "POST", "/exec",
                          {"code": "import time; time.sleep(6)", "timeout": 1})[1])
    compare("POST /exec (HTTP コード)",
            lambda i: req(i.base, "POST", "/exec", {})[0])

    compare("POST /exec/stream",
            lambda i: ndjson(i.base, "/exec/stream",
                             {"code": "for i in range(3): print('n', i)"}))

    compare("POST /upload",
            lambda i: req(i.base, "POST", "/upload",
                          {"path": "p/日本語.txt", "text": "あいう"})[1])
    compare("GET /download",
            lambda i: req(i.base, "GET", "/download?path=p/%E6%97%A5%E6%9C%AC%E8%AA%9E.txt")[1])
    compare("GET /ls", lambda i: req(i.base, "GET", "/ls?path=p")[1])
    compare("POST /upload ('..')",
            lambda i: req(i.base, "POST", "/upload",
                          {"path": "../x", "text": "x"})[1])
    compare("POST /mkdir",
            lambda i: req(i.base, "POST", "/mkdir", {"path": "p/q/r"})[1])
    compare("POST /rm",
            lambda i: req(i.base, "POST", "/rm", {"path": "p/日本語.txt"})[1])

    compare("生成コード: kbagent 注入", lambda i: agent_code_of(i))
    compare("POST /sh",
            lambda i: req(i.base, "POST", "/sh",
                          {"cmd": "echo parity-sh", "cwd": i.root})[1])
    compare("POST /sh (終了コード)",
            lambda i: req(i.base, "POST", "/sh", {"cmd": "exit 4", "cwd": i.root})[1])
    compare("POST /sh/stream",
            lambda i: ndjson(i.base, "/sh/stream",
                             {"cmd": "echo a; echo b", "cwd": i.root}))
    compare("GET /gpu", lambda i: req(i.base, "GET", "/gpu", timeout=300)[1])

    compare("POST /job",
            lambda i: req(i.base, "POST", "/job",
                          {"cmd": "echo j1; exit 2", "name": "par",
                           "cwd": i.root}, timeout=300)[1])
    time.sleep(2.0)
    compare("GET /job", lambda i: req(i.base, "GET", "/job", timeout=300)[1])
    compare("GET /job/{id}/log", lambda i: job_log_of(i))

    compare("GET /batch/status",
            lambda i: req(i.base, "GET", "/batch/status?id=u/k", timeout=300)[1])
    compare("POST /batch/push (無いディレクトリ)",
            lambda i: req(i.base, "POST", "/batch/push",
                          {"dir": "no-such-dir"}, timeout=300)[1])

    compare("DELETE /session", lambda i: req(i.base, "DELETE", "/session")[1])
    compare("GET /session (切断後)", lambda i: req(i.base, "GET", "/session")[1])


def job_log_of(impl):
    s, r = req(impl.base, "GET", "/job", timeout=300)
    if not r.get("jobs"):
        return {"error": "no jobs"}
    jid = r["jobs"][-1]["id"]
    return req(impl.base, "GET", "/job/%s/log" % jid, timeout=300)[1]


def agent_code_of(impl):
    """両実装がカーネルへ送る「注入コード」が同一かを直接見る。

    ここが一致していれば、Kaggle 側の挙動は 1 つの kbagent.py に収束する。
    python 側は import して、cpp 側は --dump-inject で出させる。
    """
    if impl.name == "python":
        sys.path.insert(0, os.path.join(ROOT, "python"))
        from kbridge import ops
        return {"inject": ops.inject_code(),
                "call": ops.call_code("log", {"job_id": "x", "offset": 0,
                                              "max_bytes": 65536}),
                "sh": ops._sh_code("echo hi", "/kaggle/working", 295.0)}
    out = subprocess.run([os.path.join(ROOT, "kbridge_server.exe"), "--dump-codegen"],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    return json.loads(out.stdout)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--exe", default="kbridge_server.exe")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    IMPLS.append(start_impl("python", 8791, 8901, a.exe, a.verbose))
    IMPLS.append(start_impl("cpp", 8792, 8902, a.exe, a.verbose))
    try:
        run()
    finally:
        for impl in IMPLS:
            impl.proc.terminate()
            try:
                impl.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                impl.proc.kill()
            impl.srv.shutdown()

    print("\n%d 一致, %d 不一致" % (len(PASS), len(FAIL)))
    if FAIL:
        print("不一致: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
