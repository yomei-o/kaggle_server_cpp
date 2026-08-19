"""CLI のパリティ試験 — python 版 CLI と cpp 版 CLI が同じ出力を出すことを縛る。

  python tests/cli_parity.py

サーバは 1 つ（cpp 版）だけ立て、そこへ 2 つの CLI から同じ操作をする。
サーバを共有するのは「CLI の違い」だけを見たいから。
"""

import argparse
import json
import os
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import fake_jupyter  # noqa: E402
from e2e import wait_up  # noqa: E402
from parity import diff, normalize  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[ OK ]" if cond else "[DIFF]", name,
                       ("  -> " + str(detail)) if detail else ""))


def run_py(base, args):
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "python"))
    return subprocess.run([sys.executable, "-m", "kbridge.cli", "--base", base] + args,
                          cwd=os.path.join(ROOT, "python"), env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)


def run_cpp(base, args):
    exe = os.path.join(ROOT, "kbridge.exe")
    return subprocess.run([exe, "--base", base] + args, cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)


def compare(name, base, args, as_json=True):
    a, b = run_py(base, args), run_cpp(base, args)
    if as_json:
        try:
            ja, jb = json.loads(a.stdout or "{}"), json.loads(b.stdout or "{}")
        except ValueError:
            check(name, False, "JSON にならない: py=%r cpp=%r"
                  % (a.stdout[:200], b.stdout[:200]))
            return
        d = diff(normalize(ja), normalize(jb))
        check(name, d is None and a.returncode == b.returncode,
              d or ("戻り値が違う %d vs %d" % (a.returncode, b.returncode)
                    if a.returncode != b.returncode else ""))
    else:
        check(name, a.stdout == b.stdout and a.returncode == b.returncode,
              "" if a.stdout == b.stdout
              else "出力が違う py=%r cpp=%r" % (a.stdout[-200:], b.stdout[-200:]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8797)
    ap.add_argument("--fake-port", type=int, default=8905)
    a = ap.parse_args()

    srv, url, root = fake_jupyter.serve(a.fake_port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % a.port
    exe = os.path.join(ROOT, "kbridge_server.exe")
    if not os.path.exists(exe):
        raise SystemExit("no kbridge_server.exe; sh cpp/build/gcc.sh "
                         "cpp/kbridge_server.cpp -o kbridge_server.exe")
    proc = subprocess.Popen([exe, "--port", str(a.port)], cwd=ROOT,
                            stdout=subprocess.DEVNULL)
    try:
        if not wait_up(base):
            raise SystemExit("server did not come up")

        compare("status", base, ["status"])
        compare("connect", base, ["connect", url, "--no-save"])
        compare("exec -q", base, ["exec", "-c", "print('cli')", "-q"])
        compare("exec (ストリーム)", base, ["exec", "-c", "print('cli-stream')"],
                as_json=False)
        compare("run (ストリーム)", base,
                ["run", "echo cli-sh", "--cwd", root], as_json=False)
        compare("run -q", base, ["run", "echo q", "--cwd", root, "-q"])
        compare("gpu", base, ["gpu"])
        compare("ls", base, ["ls"])
        compare("job list", base, ["job", "list"])
        compare("job status (無い id)", base, ["job", "status", "no-such-job"])
        compare("batch status", base, ["batch", "status", "u/k"])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        srv.shutdown()

    print("\n%d 一致, %d 不一致" % (len(PASS), len(FAIL)))
    if FAIL:
        print("不一致: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
