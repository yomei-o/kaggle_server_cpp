"""kbridge CLI — ローカルサーバ（python 版 / cpp 版のどちらでも）を叩く薄いクライアント。

    python -m kbridge.cli serve                       # サーバを起動する
    python -m kbridge.cli connect "<VSCode Compatible URL>"
    python -m kbridge.cli gpu
    python -m kbridge.cli run "nvidia-smi"            # シェル（出力を逐次表示）
    python -m kbridge.cli exec -c "print(1+1)"        # Python セル
    python -m kbridge.cli put src/train.cu src/train.cu
    python -m kbridge.cli sync ./pure work/pure       # ディレクトリごと送る
    python -m kbridge.cli job start "bash build.sh && ./train" --name lpr
    python -m kbridge.cli job log <id> --follow       # 学習ログを追う
    python -m kbridge.cli get work/best.ckpt ./best.ckpt

サーバの場所は --base（既定 http://127.0.0.1:8787）。cpp 版サーバに向けても同じように動く
（これが python 版と cpp 版が対等であることの実地確認になる）。
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = os.environ.get("KBRIDGE_BASE", "http://127.0.0.1:8787")


class CliError(Exception):
    pass


def _req(base, method, path, body=None, key=None, timeout=None, stream=False):
    url = base.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["X-Bridge-Key"] = key
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", "replace")
        try:
            obj = json.loads(payload)
        except ValueError:
            raise CliError("HTTP %d: %s" % (e.code, payload[:400]))
        if stream:
            raise CliError(obj.get("error") or payload[:400])
        return obj
    except urllib.error.URLError as e:
        raise CliError("cannot reach %s (%s)。サーバは動いている? "
                       "`python -m kbridge.cli serve` で起動する" % (base, e.reason))
    if stream:
        return r
    payload = r.read()
    return json.loads(payload.decode("utf-8")) if payload else {}


def _emit(obj):
    """応答をそのまま JSON で出し、ok:false なら終了コード 1 を返す。

    エラーも標準出力に JSON で出す（標準エラーへ流さない）。エージェントから
    使うときに、成功も失敗も同じ形で拾えるほうが扱いやすいため。cpp 版 CLI も
    同じ約束（tests/cli_parity.py が両方の出力と終了コードを突き合わせる）。
    """
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 1 if isinstance(obj, dict) and obj.get("ok") is False else 0


def _failed(obj):
    return isinstance(obj, dict) and obj.get("ok") is False


def _print_ndjson(resp, show_start=False):
    """NDJSON ストリームを人が読める形に落とす。戻り値は end イベント。"""
    end = None
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("t")
        if t == "out":
            sys.stdout.write(ev.get("d", ""))
            sys.stdout.flush()
        elif t == "result":
            sys.stdout.write(ev.get("d", "") + "\n")
        elif t == "error":
            sys.stdout.write("\n".join(ev.get("traceback") or
                                       ["%s: %s" % (ev.get("ename"), ev.get("evalue"))]))
            sys.stdout.write("\n")
        elif t == "start" and show_start:
            sys.stderr.write("--- start ---\n")
        elif t == "end":
            end = ev
    return end


# ------------------------------------------------------------------ commands

def cmd_serve(a):
    from .server import main as server_main
    argv = ["--host", a.host, "--port", str(a.port)]
    if a.api_key:
        argv += ["--api-key", a.api_key]
    if a.url:
        argv += ["--url", a.url]
    server_main(argv)


def cmd_connect(a):
    body = {}
    if a.url:
        body["url"] = a.url
    if a.new_kernel:
        body["new_kernel"] = True
    r = _req(a.base, "POST", "/session", body, a.api_key, timeout=120)
    if a.url and a.save and not _failed(r):
        path = ".kbridge.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"url": a.url}, f, indent=2)
        # URL にはトークンが入っているので、うっかり公開しないよう念を押す
        print("saved %s (トークンを含む。.gitignore 済み)" % path, file=sys.stderr)
    return _emit(r)


def cmd_status(a):
    return _emit(_req(a.base, "GET", "/healthz", key=a.api_key, timeout=30))


def cmd_gpu(a):
    return _emit(_req(a.base, "GET", "/gpu", key=a.api_key, timeout=300))


def cmd_exec(a):
    if a.code and a.file:
        raise CliError("-c と file は同時に指定できない")
    if a.file:
        src = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8").read()
    elif a.code:
        src = a.code
    else:
        raise CliError("-c CODE か file を指定する")
    body = {"code": src, "timeout": a.timeout}
    if a.quiet:
        r = _req(a.base, "POST", "/exec", body, a.api_key, timeout=a.timeout + 30)
        _emit(r)
        return 0 if r.get("status") == "ok" else 1
    resp = _req(a.base, "POST", "/exec/stream", body, a.api_key,
                timeout=a.timeout + 30, stream=True)
    end = _print_ndjson(resp)
    return 0 if (end or {}).get("status") == "ok" else 1


def cmd_run(a):
    body = {"cmd": a.cmd, "timeout": a.timeout, "cwd": a.cwd}
    if a.quiet:
        r = _req(a.base, "POST", "/sh", body, a.api_key, timeout=a.timeout + 30)
        _emit(r)
        return 0 if r.get("status") == "ok" else 1
    resp = _req(a.base, "POST", "/sh/stream", body, a.api_key,
                timeout=a.timeout + 30, stream=True)
    end = _print_ndjson(resp)
    code = (end or {}).get("exit_code")
    if code not in (0, None):
        print("exit code %s" % code, file=sys.stderr)
    return 0 if (end or {}).get("status") == "ok" else 1


def cmd_ls(a):
    return _emit(_req(a.base, "GET", "/ls?" +
                              urllib.parse.urlencode({"path": a.path}),
                              key=a.api_key, timeout=120))


def cmd_put(a):
    with open(a.local, "rb") as f:
        data = f.read()
    remote = a.remote or os.path.basename(a.local)
    body = {"path": remote, "content_b64": base64.b64encode(data).decode("ascii")}
    return _emit(_req(a.base, "POST", "/upload", body, a.api_key, timeout=600))


def cmd_get(a):
    r = _req(a.base, "GET", "/download?" +
             urllib.parse.urlencode({"path": a.remote}), key=a.api_key, timeout=600)
    if _failed(r):
        return _emit(r)
    data = base64.b64decode(r["content_b64"])
    local = a.local or os.path.basename(a.remote)
    parent = os.path.dirname(os.path.abspath(local))
    os.makedirs(parent, exist_ok=True)
    with open(local, "wb") as f:
        f.write(data)
    return _emit({"ok": True, "path": r["path"], "local": local,
                  "size": len(data)})


def cmd_sync(a):
    """ローカルのディレクトリを Kaggle 側へ丸ごと送る（学習コードの投入用）。"""
    root = os.path.abspath(a.local)
    if not os.path.isdir(root):
        raise CliError("no such directory: %s" % a.local)
    skip_dirs = {".git", "__pycache__", ".venv", "node_modules", "build", "scratch"}
    sent, total = [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in sorted(dirnames) if d not in skip_dirs]
        for fn in sorted(filenames):
            lp = os.path.join(dirpath, fn)
            if os.path.getsize(lp) > a.max_bytes:
                print("skip (too big): %s" % lp, file=sys.stderr)
                continue
            rel = os.path.relpath(lp, root).replace("\\", "/")
            remote = a.remote.strip("/") + "/" + rel if a.remote else rel
            with open(lp, "rb") as f:
                data = f.read()
            body = {"path": remote,
                    "content_b64": base64.b64encode(data).decode("ascii")}
            r = _req(a.base, "POST", "/upload", body, a.api_key, timeout=600)
            if _failed(r):
                return _emit(r)
            sent.append(remote)
            total += len(data)
            print("sent %s (%d bytes)" % (remote, len(data)), file=sys.stderr)
    return _emit({"ok": True, "files": len(sent), "bytes": total,
                  "remote": a.remote})


def cmd_job(a):
    if a.job_cmd == "start":
        body = {"name": a.name, "cwd": a.cwd}
        if a.python:
            body["code"] = (sys.stdin.read() if a.what == "-"
                            else open(a.what, encoding="utf-8").read())
        else:
            body["cmd"] = a.what
        return _emit(_req(a.base, "POST", "/job", body, a.api_key,
                                  timeout=300))
    elif a.job_cmd == "list":
        return _emit(_req(a.base, "GET", "/job", key=a.api_key, timeout=300))
    elif a.job_cmd == "status":
        return _emit(_req(a.base, "GET", "/job/" + a.id, key=a.api_key,
                                  timeout=300))
    elif a.job_cmd == "kill":
        return _emit(_req(a.base, "POST", "/job/%s/kill" % a.id, {},
                                  a.api_key, timeout=300))
    elif a.job_cmd == "rm":
        return _emit(_req(a.base, "DELETE", "/job/" + a.id, key=a.api_key,
                                  timeout=300))
    elif a.job_cmd == "log":
        offset = a.offset
        while True:
            q = urllib.parse.urlencode({"offset": offset, "max": a.max_bytes})
            r = _req(a.base, "GET", "/job/%s/log?%s" % (a.id, q),
                     key=a.api_key, timeout=300)
            if _failed(r):
                return _emit(r)
            if r.get("data"):
                sys.stdout.write(r["data"])
                sys.stdout.flush()
            offset = r["next_offset"]
            if not a.follow:
                break
            if r["state"] != "running" and offset >= r["log_size"]:
                print("\n--- %s (exit を確認するには job status) ---" % r["state"],
                      file=sys.stderr)
                break
            time.sleep(a.interval)


def cmd_batch(a):
    if a.batch_cmd == "push":
        return _emit(_req(a.base, "POST", "/batch/push", {"dir": a.dir}, a.api_key, timeout=900))
    elif a.batch_cmd == "status":
        return _emit(_req(a.base, "GET", "/batch/status?" + urllib.parse.urlencode({"id": a.id}),
                  key=a.api_key, timeout=600))
    elif a.batch_cmd == "output":
        return _emit(_req(a.base, "POST", "/batch/output", {"id": a.id, "dir": a.dir},
                  a.api_key, timeout=1800))
    elif a.batch_cmd == "pull":
        return _emit(_req(a.base, "POST", "/batch/pull", {"id": a.id, "dir": a.dir},
                  a.api_key, timeout=900))


# --------------------------------------------------------------------- parser

def build_parser():
    ap = argparse.ArgumentParser(prog="kbridge", description=__doc__.split("\n")[0])
    ap.add_argument("--base", default=DEFAULT_BASE, help="ローカルサーバの場所")
    ap.add_argument("--api-key", default=os.environ.get("KBRIDGE_API_KEY"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="ローカルサーバを起動する（python 版）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--url", default=None)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("connect", help="Kaggle の VSCode Compatible URL に接続する")
    p.add_argument("url", nargs="?")
    p.add_argument("--new-kernel", action="store_true",
                   help="既存カーネルを再利用せず新規に作る")
    p.add_argument("--no-save", dest="save", action="store_false",
                   help=".kbridge.json に URL を保存しない")
    p.set_defaults(fn=cmd_connect, save=True)

    sub.add_parser("status", help="サーバと接続の状態").set_defaults(fn=cmd_status)
    sub.add_parser("gpu", help="GPU の状態").set_defaults(fn=cmd_gpu)

    p = sub.add_parser("exec", help="Python コードを 1 セルとして実行する")
    p.add_argument("file", nargs="?", help=".py ファイル（- で標準入力）")
    p.add_argument("-c", "--code")
    p.add_argument("--timeout", type=float, default=300)
    p.add_argument("-q", "--quiet", action="store_true", help="JSON だけ出す")
    p.set_defaults(fn=cmd_exec)

    p = sub.add_parser("run", help="シェルコマンドを実行する")
    p.add_argument("cmd")
    p.add_argument("--cwd", default="/kaggle/working")
    p.add_argument("--timeout", type=float, default=300)
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("ls", help="Kaggle 側のファイル一覧")
    p.add_argument("path", nargs="?", default="")
    p.set_defaults(fn=cmd_ls)

    p = sub.add_parser("put", help="ファイルを送る")
    p.add_argument("local")
    p.add_argument("remote", nargs="?")
    p.set_defaults(fn=cmd_put)

    p = sub.add_parser("get", help="ファイルを取る")
    p.add_argument("remote")
    p.add_argument("local", nargs="?")
    p.set_defaults(fn=cmd_get)

    p = sub.add_parser("sync", help="ディレクトリを丸ごと送る")
    p.add_argument("local")
    p.add_argument("remote", nargs="?", default="")
    p.add_argument("--max-bytes", type=int, default=32 * 1024 * 1024)
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("job", help="長時間ジョブ（学習はこれ）")
    js = p.add_subparsers(dest="job_cmd", required=True)
    q = js.add_parser("start")
    q.add_argument("what", help="シェルコマンド、または --python のとき .py ファイル/-")
    q.add_argument("--name", default="job")
    q.add_argument("--cwd", default="/kaggle/working")
    q.add_argument("--python", action="store_true")
    js.add_parser("list")
    for name in ("status", "kill", "rm"):
        q = js.add_parser(name)
        q.add_argument("id")
    q = js.add_parser("log")
    q.add_argument("id")
    q.add_argument("--offset", type=int, default=0)
    q.add_argument("--max-bytes", type=int, default=65536)
    q.add_argument("-f", "--follow", action="store_true")
    q.add_argument("--interval", type=float, default=5.0)
    p.set_defaults(fn=cmd_job)

    p = sub.add_parser("batch", help="kaggle CLI 経由のバッチ実行")
    bs = p.add_subparsers(dest="batch_cmd", required=True)
    q = bs.add_parser("push"); q.add_argument("dir")
    q = bs.add_parser("status"); q.add_argument("id")
    q = bs.add_parser("output"); q.add_argument("id"); q.add_argument("dir")
    q = bs.add_parser("pull"); q.add_argument("id"); q.add_argument("dir")
    p.set_defaults(fn=cmd_batch)

    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except CliError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
