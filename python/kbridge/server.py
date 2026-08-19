"""kbridge ローカルサーバ（FastAPI 版）。spec/API.md の実装。

  python -m kbridge.server --port 8787
  python -m kbridge.cli serve --port 8787        # 同じもの

C++ 版（cpp/kbridge_server.cpp）と URL・JSON・挙動を合わせること。
仕様を変えるときは spec/API.md → 両実装 → tests/parity.py の順で直す。
"""

import argparse
import base64
import json
import os
import threading
import time

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import IMPL, VERSION, batch, ops
from .jupyter import JupyterClient, JupyterError

STARTED = time.time()


class State:
    """接続中の Kaggle セッション 1 つ分。"""

    def __init__(self):
        self.client = None
        self.url = None
        self.lock = threading.Lock()   # カーネルは 1 度に 1 実行しか受けない

    def need(self):
        if self.client is None or self.client.kernel_id is None:
            raise SessionMissing()
        return self.client


class SessionMissing(Exception):
    pass


state = State()


def default_url():
    """--url も body も無いときの既定。環境変数 -> .kbridge.json の順。"""
    env = os.environ.get("KAGGLE_JUPYTER_URL")
    if env:
        return env.strip()
    for path in (".kbridge.json",
                 os.path.join(os.path.expanduser("~"), ".kbridge.json")):
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    u = json.load(f).get("url")
                if u:
                    return u.strip()
            except (OSError, ValueError):
                pass
    return None


def err(message, status=400, **extra):
    body = {"ok": False, "error": str(message)}
    body.update(extra)
    return JSONResponse(body, status_code=status)


def create_app(api_key=None):
    app = FastAPI(title="kbridge", version=VERSION, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if api_key and request.headers.get("X-Bridge-Key") != api_key:
            return err("bad or missing X-Bridge-Key", 401)
        try:
            return await call_next(request)
        except SessionMissing:
            return err("no session; POST /session first", 409)
        except JupyterError as e:
            return err(e, 502)
        except ValueError as e:
            return err(e, 400)

    async def body_of(request):
        raw = await request.body()
        if not raw:
            return {}
        try:
            obj = json.loads(raw)
        except ValueError:
            raise ValueError("body must be JSON")
        if not isinstance(obj, dict):
            raise ValueError("body must be a JSON object")
        return obj

    def safe_path(p):
        p = (p or "").strip().replace("\\", "/").strip("/")
        if ".." in p.split("/"):
            raise ValueError("path must not contain '..'")
        return p

    # ------------------------------------------------------------- セッション
    @app.get("/healthz")
    def healthz():
        c = state.client
        return {"ok": True, "impl": IMPL, "version": VERSION,
                "connected": bool(c and c.kernel_id and c._ws),
                "kernel_id": c.kernel_id if c else None,
                "uptime": round(time.time() - STARTED, 3)}

    @app.post("/session")
    async def session_new(request: Request):
        body = await body_of(request)
        url = (body.get("url") or default_url() or "").strip()
        if not url:
            raise ValueError(
                "no url: give {\"url\": ...}, or set KAGGLE_JUPYTER_URL, "
                "or put it in .kbridge.json")
        client = JupyterClient(url)
        info = client.connect(new_kernel=bool(body.get("new_kernel")))
        with state.lock:
            if state.client is not None:
                state.client.disconnect()
            state.client = client
            state.url = url
        return {"ok": True, **info}

    @app.get("/session")
    def session_get():
        return {"ok": True, **state.need().info()}

    @app.delete("/session")
    def session_del():
        c = state.need()
        c.shutdown()
        state.client = None
        return {"ok": True}

    @app.post("/interrupt")
    def interrupt():
        state.need().interrupt()
        return {"ok": True}

    @app.post("/restart")
    def restart():
        c = state.need()
        return {"ok": True, "kernel_id": c.restart()}

    # ------------------------------------------------------------------ 実行
    def exec_timeout(body):
        t = float(body.get("timeout", 300))
        if not 0 < t <= 43200:
            raise ValueError("timeout must be in (0, 43200]")
        return t

    @app.post("/exec")
    async def do_exec(request: Request):
        body = await body_of(request)
        code = body.get("code")
        if not isinstance(code, str) or not code:
            raise ValueError("code is required")
        c = state.need()
        with state.lock:
            r = c.execute(code, timeout=exec_timeout(body))
        return JSONResponse(r, status_code=504 if r["status"] == "timeout" else 200)

    def ndjson(gen):
        return StreamingResponse(gen, media_type="application/x-ndjson")

    def stream_exec(code, timeout, wrap=None):
        """execute を別スレッドで回し、届いたイベントを NDJSON で流す。"""
        import queue as _q
        c = state.need()
        q = _q.Queue()

        def emit(ev):
            q.put(ev)

        sink = wrap(emit) if wrap else None
        feed = sink.feed if sink else emit

        def run():
            try:
                r = c.execute(code, timeout=timeout, on_event=feed)
                if sink:
                    sink.flush()
                    marker = sink.marker or {}
                    r["exit_code"] = marker.get("exit_code")
                    if marker.get("timeout"):
                        r["status"], r["ok"] = "timeout", False
                    elif r["status"] == "ok" and r["exit_code"] not in (0, None):
                        r["status"], r["ok"] = "error", False
                end = {"t": "end", "status": r["status"],
                       "execution_count": r["execution_count"],
                       "elapsed": r["elapsed"]}
                if sink:
                    end["exit_code"] = r.get("exit_code")
                q.put(end)
            except Exception as e:                      # 上流断など
                q.put({"t": "end", "status": "abort", "error": str(e),
                       "execution_count": None, "elapsed": 0.0})
            finally:
                q.put(None)

        def gen():
            with state.lock:
                th = threading.Thread(target=run, daemon=True)
                th.start()
                yield json.dumps({"t": "start"}, ensure_ascii=False) + "\n"
                while True:
                    ev = q.get()
                    if ev is None:
                        break
                    yield json.dumps(ev, ensure_ascii=False) + "\n"
                th.join()

        return ndjson(gen())

    @app.post("/exec/stream")
    async def do_exec_stream(request: Request):
        body = await body_of(request)
        code = body.get("code")
        if not isinstance(code, str) or not code:
            raise ValueError("code is required")
        return stream_exec(code, exec_timeout(body))

    @app.post("/sh")
    async def do_sh(request: Request):
        body = await body_of(request)
        cmd = body.get("cmd")
        if not isinstance(cmd, str) or not cmd:
            raise ValueError("cmd is required")
        c = state.need()
        t = exec_timeout(body)
        with state.lock:
            r = ops.sh(c, cmd, cwd=body.get("cwd", "/kaggle/working"), timeout=t)
        return JSONResponse(r, status_code=504 if r["status"] == "timeout" else 200)

    @app.post("/sh/stream")
    async def do_sh_stream(request: Request):
        body = await body_of(request)
        cmd = body.get("cmd")
        if not isinstance(cmd, str) or not cmd:
            raise ValueError("cmd is required")
        c = state.need()
        t = exec_timeout(body)
        ops.ensure_agent(c)
        code = ops._sh_code(cmd, body.get("cwd", "/kaggle/working"), max(t - 5.0, 1.0))
        return stream_exec(code, t, wrap=ops.ShellStream)

    @app.get("/gpu")
    def do_gpu():
        c = state.need()
        with state.lock:
            return {"ok": True, **ops.gpu(c)}

    # ---------------------------------------------------------------- ファイル
    @app.post("/upload")
    async def upload(request: Request):
        body = await body_of(request)
        path = safe_path(body.get("path"))
        if not path:
            raise ValueError("path is required")
        has_text, has_b64 = "text" in body, "content_b64" in body
        if has_text == has_b64:
            raise ValueError("give exactly one of text or content_b64")
        data = (body["text"].encode("utf-8") if has_text
                else base64.b64decode(body["content_b64"]))
        c = state.need()
        with state.lock:
            size = c.put_file(path, data)
        return {"ok": True, "path": path, "size": size}

    @app.get("/download")
    def download(path: str, raw: int = 0):
        p = safe_path(path)
        c = state.need()
        with state.lock:
            data = c.get_file(p)
        if raw:
            return Response(content=data, media_type="application/octet-stream")
        return {"ok": True, "path": p, "size": len(data),
                "content_b64": base64.b64encode(data).decode("ascii")}

    @app.get("/ls")
    def do_ls(path: str = ""):
        c = state.need()
        with state.lock:
            return {"ok": True, **c.ls(safe_path(path))}

    @app.post("/mkdir")
    async def do_mkdir(request: Request):
        body = await body_of(request)
        path = safe_path(body.get("path"))
        if not path:
            raise ValueError("path is required")
        c = state.need()
        with state.lock:
            c.mkdirs(path)
        return {"ok": True, "path": path}

    @app.post("/rm")
    async def do_rm(request: Request):
        body = await body_of(request)
        path = safe_path(body.get("path"))
        if not path:
            raise ValueError("path is required")
        c = state.need()
        with state.lock:
            c.rm(path)
        return {"ok": True, "path": path}

    # -------------------------------------------------------------------- ジョブ
    @app.post("/job")
    async def job_new(request: Request):
        body = await body_of(request)
        cmd, code = body.get("cmd"), body.get("code")
        if (cmd is None) == (code is None):
            raise ValueError("give exactly one of cmd or code")
        c = state.need()
        with state.lock:
            j = ops.job_start(c, cmd=cmd, code=code, name=body.get("name", "job"),
                              cwd=body.get("cwd", "/kaggle/working"),
                              env=body.get("env"))
        return {"ok": True, **j}

    @app.get("/job")
    def job_list():
        c = state.need()
        with state.lock:
            return {"ok": True, "jobs": ops.job_list(c)}

    @app.get("/job/{job_id}")
    def job_one(job_id: str):
        c = state.need()
        with state.lock:
            return {"ok": True, **ops.job_status(c, job_id)}

    @app.get("/job/{job_id}/log")
    def job_log(job_id: str, offset: int = 0, max: int = 65536):
        c = state.need()
        with state.lock:
            return {"ok": True, **ops.job_log(c, job_id, offset=offset,
                                              max_bytes=max)}

    @app.post("/job/{job_id}/kill")
    def job_kill(job_id: str):
        c = state.need()
        with state.lock:
            return {"ok": True, **ops.job_kill(c, job_id)}

    @app.delete("/job/{job_id}")
    def job_rm(job_id: str):
        c = state.need()
        with state.lock:
            return {"ok": True, **ops.job_rm(c, job_id)}

    # -------------------------------------------------------------------- バッチ
    def batch_guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except batch.BatchUnavailable as e:
            return {"ok": False, "error": str(e)}

    @app.post("/batch/push")
    async def batch_push(request: Request):
        body = await body_of(request)
        d = body.get("dir")
        if not d:
            raise ValueError("dir is required")
        return batch_guard(batch.push, d)

    @app.get("/batch/status")
    def batch_status(id: str):
        return batch_guard(batch.status, id)

    @app.post("/batch/output")
    async def batch_output(request: Request):
        body = await body_of(request)
        if not body.get("id") or not body.get("dir"):
            raise ValueError("id and dir are required")
        return batch_guard(batch.output, body["id"], body["dir"])

    @app.post("/batch/pull")
    async def batch_pull(request: Request):
        body = await body_of(request)
        if not body.get("id") or not body.get("dir"):
            raise ValueError("id and dir are required")
        return batch_guard(batch.pull, body["id"], body["dir"])

    return app


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kbridge-server",
                                 description="Kaggle Jupyter Server ブリッジ（python 版）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--api-key", default=os.environ.get("KBRIDGE_API_KEY"),
                    help="付けると X-Bridge-Key ヘッダを必須にする")
    ap.add_argument("--url", default=None,
                    help="起動時に接続する VSCode Compatible URL")
    args = ap.parse_args(argv)

    import uvicorn
    app = create_app(api_key=args.api_key)

    if args.url or default_url():
        url = args.url or default_url()
        try:
            client = JupyterClient(url)
            info = client.connect()
            state.client, state.url = client, url
            print("connected: %s kernel=%s (reuse=%s)"
                  % (info["base_url"], info["kernel_id"], info["reuse"]))
        except Exception as e:      # 起動は続ける。後から POST /session できる
            print("startup connect failed: %s" % e)

    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.api_key:
        print("warning: loopback 以外に bind するなら --api-key を付けること")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
