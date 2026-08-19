"""kbridge のエンドツーエンド試験。偽 Jupyter サーバ相手に REST 仕様を一通り叩く。

  python tests/e2e.py                       # python 版サーバを自分で起動して試験
  python tests/e2e.py --impl cpp            # cpp 版サーバ（kbridge_server.exe）を試験
  python tests/e2e.py --base http://127.0.0.1:8787 --no-spawn   # 動いているサーバを試験

Kaggle には一切つながないので、GPU 無料枠を消費しない。ここが通っていれば、残る差分は
「本物の Kaggle が返すもの」だけになる。
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "python"))
sys.path.insert(0, HERE)

import fake_jupyter  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[ OK ]" if cond else "[FAIL]", name,
                       ("  -> " + str(detail)) if detail else ""))
    return cond


def req(base, method, path, body=None, timeout=120, raw=False):
    url = base.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            payload = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        payload, status = e.read(), e.code
    if raw:
        return status, payload
    try:
        return status, json.loads(payload.decode("utf-8"))
    except ValueError:
        return status, payload.decode("utf-8", "replace")


def ndjson(base, path, body, timeout=300):
    url = base.rstrip("/") + path
    r = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                               method="POST",
                               headers={"Content-Type": "application/json"})
    events = []
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        for line in resp:
            line = line.decode("utf-8", "replace").strip()
            if line:
                events.append(json.loads(line))
    return events


def wait_up(base, seconds=30):
    end = time.time() + seconds
    while time.time() < end:
        try:
            s, _ = req(base, "GET", "/healthz", timeout=3)
            if s == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def run_tests(base, url, root, want_impl):
    # --- セッション ---------------------------------------------------------
    s, h = req(base, "GET", "/healthz")
    check("healthz", s == 200 and h.get("ok") and h.get("impl") == want_impl, h)

    s, r = req(base, "POST", "/session", {"url": url})
    check("POST /session", s == 200 and r.get("ok") and r.get("kernel_id"), r)
    check("token がマスクされている", "TESTTOKEN" not in json.dumps(r), r.get("base_url"))

    s, r = req(base, "GET", "/session")
    check("GET /session", s == 200 and r.get("kernel_id"), r)

    # --- exec ---------------------------------------------------------------
    s, r = req(base, "POST", "/exec", {"code": "print('hello'); x = 40 + 2"})
    check("POST /exec", s == 200 and r["status"] == "ok" and r["stdout"] == "hello\n", r)

    s, r = req(base, "POST", "/exec", {"code": "print(x)"})
    check("カーネルの状態が続いている", s == 200 and r["stdout"].strip() == "42", r)

    s, r = req(base, "POST", "/exec", {"code": "raise RuntimeError('boom')"})
    check("例外が error になる",
          s == 200 and r["status"] == "error" and r["ename"] == "RuntimeError", r)

    s, r = req(base, "POST", "/exec", {"code": "import sys; print('e', file=sys.stderr)"})
    check("stderr が分かれている", s == 200 and r["stderr"].strip() == "e", r)

    s, r = req(base, "POST", "/exec", {"code": "import time; time.sleep(30)",
                                       "timeout": 2})
    check("timeout で 504 と status=timeout", s == 504 and r["status"] == "timeout", r)

    s, r = req(base, "POST", "/exec", {})
    check("code 無しは 400", s == 400 and r.get("ok") is False, r)

    # --- exec/stream --------------------------------------------------------
    evs = ndjson(base, "/exec/stream",
                 {"code": "import time\nfor i in range(3):\n"
                          "    print('tick', i, flush=True)\n    time.sleep(0.2)\n"})
    kinds = [e["t"] for e in evs]
    text = "".join(e.get("d", "") for e in evs if e["t"] == "out")
    check("exec/stream の形", kinds[0] == "start" and kinds[-1] == "end", kinds)
    check("exec/stream の中身", "tick 0" in text and "tick 2" in text, repr(text))
    check("exec/stream の end", evs[-1]["status"] == "ok", evs[-1])

    # --- ファイル -----------------------------------------------------------
    s, r = req(base, "POST", "/upload", {"path": "src/hello.txt", "text": "こんにちは"})
    check("POST /upload (text)", s == 200 and r["size"] == len("こんにちは".encode()), r)

    s, r = req(base, "GET", "/download?" + urllib.parse.urlencode({"path": "src/hello.txt"}))
    check("GET /download",
          s == 200 and base64.b64decode(r["content_b64"]).decode() == "こんにちは", r)

    blob = bytes(range(256)) * 8
    s, r = req(base, "POST", "/upload",
               {"path": "src/blob.bin",
                "content_b64": base64.b64encode(blob).decode()})
    check("POST /upload (binary)", s == 200 and r["size"] == len(blob), r)
    s, raw = req(base, "GET", "/download?" +
                 urllib.parse.urlencode({"path": "src/blob.bin", "raw": 1}), raw=True)
    check("GET /download?raw=1 がバイト一致", s == 200 and raw == blob, len(raw))

    s, r = req(base, "GET", "/ls?path=src")
    names = sorted(e["name"] for e in r.get("entries", []))
    check("GET /ls", s == 200 and names == ["blob.bin", "hello.txt"], names)

    s, r = req(base, "POST", "/upload", {"path": "../escape.txt", "text": "x"})
    check("'..' を弾く", s == 400 and r.get("ok") is False, r)

    s, r = req(base, "POST", "/upload", {"path": "a.txt", "text": "x",
                                         "content_b64": "eA=="})
    check("text と content_b64 の同時指定を弾く", s == 400, r)

    s, r = req(base, "POST", "/rm", {"path": "src/blob.bin"})
    check("POST /rm", s == 200 and r["ok"], r)

    # --- sh -----------------------------------------------------------------
    s, r = req(base, "POST", "/sh", {"cmd": "echo shell-ok", "cwd": root})
    check("POST /sh",
          s == 200 and "shell-ok" in r.get("stdout", "") and r.get("exit_code") == 0, r)
    check("/sh の出力にマーカーが混ざらない",
          "__KBRIDGE_EXIT__" not in r.get("stdout", ""), repr(r.get("stdout")))

    s, r = req(base, "POST", "/sh", {"cmd": "exit 3", "cwd": root})
    check("/sh が終了コードを返す",
          s == 200 and r.get("exit_code") == 3 and r.get("ok") is False, r)

    evs = ndjson(base, "/sh/stream",
                 {"cmd": "for i in 1 2 3; do echo s$i; sleep 0.2; done", "cwd": root})
    text = "".join(e.get("d", "") for e in evs if e["t"] == "out")
    check("/sh/stream の中身", "s1" in text and "s3" in text, repr(text))
    check("/sh/stream にマーカーが混ざらない", "__KBRIDGE_EXIT__" not in text, repr(text))
    check("/sh/stream の end に exit_code",
          evs[-1]["t"] == "end" and evs[-1].get("exit_code") == 0, evs[-1])

    # --- gpu ----------------------------------------------------------------
    s, r = req(base, "GET", "/gpu", timeout=300)
    check("GET /gpu (GPU 無しでも ok)",
          s == 200 and r["ok"] and isinstance(r["gpus"], list), r.get("gpus"))

    # --- job ----------------------------------------------------------------
    s, j = req(base, "POST", "/job",
               {"cmd": "for i in 1 2 3; do echo job$i; sleep 0.4; done; exit 5",
                "name": "e2e", "cwd": root}, timeout=300)
    check("POST /job", s == 200 and j.get("id") and j["state"] == "running", j)
    job_id = j.get("id")

    s, r = req(base, "GET", "/job", timeout=300)
    check("GET /job", s == 200 and any(x["id"] == job_id for x in r["jobs"]), r)

    seen, offset, state = "", 0, "running"
    for _ in range(40):
        s, r = req(base, "GET", "/job/%s/log?offset=%d" % (job_id, offset), timeout=300)
        seen += r.get("data", "")
        offset = r["next_offset"]
        state = r["state"]
        if state != "running" and r["eof"]:
            break
        time.sleep(0.5)
    check("ジョブのログを増分で追える", "job1" in seen and "job3" in seen, repr(seen))
    check("ジョブが終了して状態が変わる", state in ("failed", "done"), state)

    s, r = req(base, "GET", "/job/" + job_id, timeout=300)
    check("ジョブの終了コード", r.get("exit_code") == 5, r)

    s, r = req(base, "DELETE", "/job/" + job_id, timeout=300)
    check("DELETE /job/{id}", s == 200 and r["ok"], r)

    # --- batch（kaggle CLI が無い環境では ok:false を返すのが正しい） --------
    s, r = req(base, "GET", "/batch/status?id=user/kernel", timeout=300)
    check("batch は CLI 不在でも 200 で ok:false",
          s == 200 and ("ok" in r), r)

    # --- 後始末 -------------------------------------------------------------
    s, r = req(base, "DELETE", "/session")
    check("DELETE /session", s == 200 and r["ok"], r)
    s, r = req(base, "GET", "/session")
    check("切断後は 409", s == 409, r)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--impl", choices=("python", "cpp"), default="python")
    ap.add_argument("--base", default=None)
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--fake-port", type=int, default=8899)
    ap.add_argument("--exe", default="kbridge_server.exe", help="cpp 版の実行ファイル")
    ap.add_argument("--no-spawn", action="store_true", help="サーバを自分で起動しない")
    a = ap.parse_args()

    srv, url, root = fake_jupyter.serve(a.fake_port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("fake jupyter: %s  (root %s)" % (url, root))

    base = a.base or "http://127.0.0.1:%d" % a.port
    proc = None
    if not a.no_spawn:
        if a.impl == "python":
            cmd = [sys.executable, "-m", "kbridge.server", "--port", str(a.port)]
            env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "python"))
            proc = subprocess.Popen(cmd, cwd=os.path.join(ROOT, "python"), env=env)
        else:
            exe = a.exe if os.path.isabs(a.exe) else os.path.join(ROOT, a.exe)
            if not os.path.exists(exe):
                print("no such exe: %s  (sh cpp/build/gcc.sh cpp/kbridge_server.cpp "
                      "-o kbridge_server.exe)" % exe)
                return 2
            proc = subprocess.Popen([exe, "--port", str(a.port)], cwd=ROOT)
        if not wait_up(base):
            print("server did not come up: %s" % base)
            if proc:
                proc.terminate()
            return 2

    try:
        run_tests(base, url, root, a.impl)
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        srv.shutdown()

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
