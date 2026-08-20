"""keep-alive スレッドの試験（偽 Jupyter 相手）。

  python tests/keepalive.py                 # python 版
  python tests/keepalive.py --impl cpp      # cpp 版

本番の既定は 240 秒なので待てない。ここでは --keepalive 2 で回して
「アイドルが続けばカーネルへ execute_request が飛ぶ」「0 なら飛ばない」だけを見る。
Kaggle の idle タイマーが実際に戻るかは本物で 40 分放置しないと分からない（README 参照）。
"""

import argparse
import os
import subprocess
import sys
import threading
import time
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


def wait_up(base, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def spawn(impl, port, exe, url, keepalive):
    """サーバを起動して (proc, 出力を溜めるリスト) を返す。"""
    if impl == "python":
        cmd = [sys.executable, "-u", "-m", "kbridge.server", "--port", str(port),
               "--keepalive", str(keepalive), "--url", url]
        cwd, env = os.path.join(ROOT, "python"), dict(
            os.environ, PYTHONPATH=os.path.join(ROOT, "python"))
    else:
        exe = exe if os.path.isabs(exe) else os.path.join(ROOT, exe)
        if not os.path.exists(exe):
            print("no such exe: %s" % exe)
            return None, None
        cmd = [exe, "--port", str(port), "--keepalive", str(keepalive),
               "--url", url]
        cwd, env = ROOT, dict(os.environ)

    lines = []
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)

    def drain():
        for line in proc.stdout:
            lines.append(line.rstrip())
    threading.Thread(target=drain, daemon=True).start()
    return proc, lines


def stop(proc):
    if not proc:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def ticks(lines):
    return [l for l in lines if "keepalive #" in l]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--impl", choices=("python", "cpp"), default="python")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--fake-port", type=int, default=8898)
    ap.add_argument("--exe", default="kbridge_server.exe")
    a = ap.parse_args()

    srv, url, root = fake_jupyter.serve(a.fake_port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % a.port

    try:
        # --- 有効時: アイドルを放っておけば勝手に打つ -----------------------
        proc, lines = spawn(a.impl, a.port, a.exe, url, 2)
        if proc is None:
            return 2
        try:
            if not check("server comes up (keepalive 2s)", wait_up(base)):
                return 2
            check("startup line says the interval",
                  any("keepalive" in l and "2" in l for l in lines),
                  [l for l in lines if "keepalive" in l])
            time.sleep(7.0)
            n = len(ticks(lines))
            check("ticks fire while idle (>=2 in 7s)", n >= 2, "%d ticks" % n)
            check("tick reports ok", all("ok" in l for l in ticks(lines)),
                  ticks(lines))
        finally:
            stop(proc)

        # --- 無効時: 1 度も打たない ----------------------------------------
        proc, lines = spawn(a.impl, a.port, a.exe, url, 0)
        try:
            if not check("server comes up (keepalive off)", wait_up(base)):
                return 2
            time.sleep(5.0)
            check("no ticks when disabled", not ticks(lines), ticks(lines))
        finally:
            stop(proc)
    finally:
        srv.shutdown()

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
