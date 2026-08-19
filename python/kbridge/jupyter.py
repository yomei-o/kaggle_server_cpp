"""Jupyter Server クライアント（REST + カーネル WebSocket プロトコル 5.3）。

Kaggle の "VSCode Compatible URL" に対して、VS Code の Jupyter 拡張がやっていることを
自前でやる。C++ 版 cpp/pure/jupyter.hpp と同じ手順・同じ結果になるように実装している。

  1. GET  <base>/                          -> Cookie の _xsrf を取得
  2. GET  <base>/api/kernels               -> 既存カーネルがあれば再利用
                                              （Kaggle の GPU を握っているのは Notebook 本体のカーネル）
     無ければ POST <base>/api/sessions     -> 新規カーネル
  3. WSS  <base>/api/kernels/<kid>/channels?session_id=... で常時接続
  4. execute_request を投げ、parent_header.msg_id で自分宛の出力だけ拾う

サブプロトコル v1.kernel.websocket.jupyter.org は**要求しない**。要求するとメッセージが
バイナリ多重化形式になり、JSON テキストで読めなくなるため。
"""

import base64
import json
import os
import queue
import threading
import time
import uuid
from urllib.parse import quote, urlencode, urlparse
import urllib.error
import urllib.request

from .jurl import parse_jupyter_url, mask_base
from .ws import WebSocketClient, WebSocketClosed

PROTOCOL_VERSION = "5.3"
USER_AGENT = ("Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:135.0) "
              "Gecko/20100101 Firefox/135.0")


class JupyterError(Exception):
    """上流 (Kaggle) 側のエラー。HTTP 502 相当。"""


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000Z"


class JupyterClient:
    def __init__(self, url, http_timeout=60.0):
        self.base_url, self.token = parse_jupyter_url(url)
        self.http_timeout = http_timeout
        self.session_id = uuid.uuid4().hex
        self.kernel_id = None
        self.kernel_name = None
        self.reused = False
        self.xsrf = None
        self._cookies = {}
        self._ws = None
        self._ws_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._subs = {}          # msg_id -> queue.Queue
        self._subs_lock = threading.Lock()
        self._reader = None
        self._stop = threading.Event()
        self._ws_error = None

    # --------------------------------------------------------------- display
    @property
    def safe_base(self):
        return mask_base(self.base_url, self.token)

    def info(self):
        return {"base_url": self.safe_base, "kernel_id": self.kernel_id,
                "session_id": self.session_id, "kernel_name": self.kernel_name,
                "reuse": self.reused,
                "connected": self.kernel_id is not None and self._ws is not None}

    # ------------------------------------------------------------------ HTTP
    def _url(self, *parts, **query):
        path = "/".join(str(p).strip("/") for p in parts if p is not None)
        url = self.base_url + ("/" + path if path else "/")
        q = dict(query)
        if self.token and "token" not in q:
            q["token"] = self.token
        if q:
            url += "?" + urlencode(q)
        return url

    def _headers(self, extra=None):
        h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.token:
            h["Authorization"] = "token %s" % self.token
        if self.xsrf:
            h["X-XSRFToken"] = self.xsrf
        if self._cookies:
            h["Cookie"] = "; ".join("%s=%s" % kv for kv in self._cookies.items())
        if extra:
            h.update(extra)
        return h

    def _request(self, method, url, body=None, timeout=None, raw=False):
        data = None
        extra = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            extra["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=self._headers(extra))
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.http_timeout) as r:
                self._absorb_cookies(r.headers.get_all("Set-Cookie") or [])
                payload = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise JupyterError("%s %s -> HTTP %d: %s"
                               % (method, self._mask(url), e.code, detail))
        except Exception as e:
            raise JupyterError("%s %s -> %s" % (method, self._mask(url), e))
        if raw:
            return payload
        if not payload:
            return None
        try:
            return json.loads(payload.decode("utf-8"))
        except ValueError:
            return payload.decode("utf-8", "replace")

    def _mask(self, url):
        return mask_base(url, self.token)

    def _absorb_cookies(self, set_cookies):
        for sc in set_cookies:
            kv = sc.split(";", 1)[0].strip()
            if "=" in kv:
                k, v = kv.split("=", 1)
                self._cookies[k] = v
                if k == "_xsrf":
                    self.xsrf = v

    # -------------------------------------------------------------- lifecycle
    def connect(self, new_kernel=False):
        # 1. XSRF cookie（トップページが 404 でも Cookie は付いてくることがある）
        try:
            self._request("GET", self._url(), raw=True)
        except JupyterError:
            pass

        # 2. カーネル
        if not new_kernel:
            try:
                kernels = self._request("GET", self._url("api", "kernels")) or []
            except JupyterError:
                kernels = []
            alive = [k for k in kernels if k.get("execution_state") != "dead"]
            if alive:
                self.kernel_id = alive[0]["id"]
                self.kernel_name = alive[0].get("name", "python3")
                self.reused = True
        if not self.kernel_id:
            path = "kbridge-%s.ipynb" % self.session_id[:8]
            body = {"path": path, "type": "notebook", "name": "",
                    "kernel": {"name": "python3"}}
            sess = self._request("POST", self._url("api", "sessions"), body=body)
            if not sess or "kernel" not in sess:
                raise JupyterError("could not create session: %r" % (sess,))
            self.kernel_id = sess["kernel"]["id"]
            self.kernel_name = sess["kernel"].get("name", "python3")
            self.reused = False

        self._connect_ws()
        return self.info()

    def _ws_url(self):
        p = urlparse(self.base_url)
        scheme = "wss" if p.scheme == "https" else "ws"
        return "%s://%s%s/api/kernels/%s/channels?%s" % (
            scheme, p.netloc, p.path.rstrip("/"), self.kernel_id,
            urlencode({"session_id": self.session_id}))

    def _connect_ws(self):
        with self._ws_lock:
            if self._ws is not None:
                return
            p = urlparse(self.base_url)
            headers = {"Origin": "%s://%s" % (p.scheme, p.netloc),
                       "Cache-Control": "no-cache", "Pragma": "no-cache"}
            if self.token:
                headers["Authorization"] = "token %s" % self.token
            if self._cookies:
                headers["Cookie"] = "; ".join("%s=%s" % kv
                                              for kv in self._cookies.items())
            ws = WebSocketClient(self._ws_url(), headers=headers)
            ws.connect()
            self._ws = ws
            self._ws_error = None
            self._stop.clear()
            self._reader = threading.Thread(target=self._reader_loop,
                                            name="kbridge-ws-reader", daemon=True)
            self._reader.start()

    def _reader_loop(self):
        while not self._stop.is_set():
            try:
                msg = self._ws.recv(timeout=1.0)
            except WebSocketClosed as e:
                self._ws_error = str(e)
                break
            except Exception as e:
                self._ws_error = "%s: %s" % (type(e).__name__, e)
                break
            if msg is None or isinstance(msg, bytes):
                continue
            try:
                obj = json.loads(msg)
            except ValueError:
                continue
            parent = (obj.get("parent_header") or {}).get("msg_id")
            with self._subs_lock:
                q = self._subs.get(parent)
            if q is not None:
                q.put(obj)

        with self._ws_lock:
            if self._ws is not None:
                self._ws.close()
                self._ws = None
        with self._subs_lock:      # 待っている実行を起こす
            for q in self._subs.values():
                q.put(None)

    def ensure(self):
        if self.kernel_id is None:
            raise JupyterError("no session; POST /session first")
        if self._ws is None:
            self._connect_ws()

    def disconnect(self):
        self._stop.set()
        with self._ws_lock:
            if self._ws is not None:
                self._ws.close()
                self._ws = None

    def shutdown(self):
        self.disconnect()
        if self.kernel_id:
            try:
                self._request("DELETE", self._url("api", "kernels", self.kernel_id))
            except JupyterError:
                pass
        self.kernel_id = None

    def interrupt(self):
        self.ensure()
        self._request("POST", self._url("api", "kernels", self.kernel_id, "interrupt"))

    def restart(self):
        self.ensure()
        self.disconnect()
        r = self._request("POST", self._url("api", "kernels", self.kernel_id, "restart"))
        if isinstance(r, dict) and r.get("id"):
            self.kernel_id = r["id"]
        self._connect_ws()
        return self.kernel_id

    # --------------------------------------------------------------- execute
    def execute(self, code, timeout=300.0, on_event=None):
        """コードを 1 セルとして実行する。

        on_event(ev) を渡すと {"t":"out"/"result"/"error", ...} を逐次呼ぶ
        （spec/API.md の NDJSON と同じ形）。戻り値は /exec の応答そのもの。
        """
        self.ensure()
        msg_id = uuid.uuid4().hex
        msg = {"header": {"msg_id": msg_id, "username": "kbridge",
                          "session": self.session_id, "date": _now_iso(),
                          "msg_type": "execute_request", "version": PROTOCOL_VERSION},
               "parent_header": {}, "metadata": {},
               "content": {"code": code, "silent": False, "store_history": True,
                           "user_expressions": {}, "allow_stdin": False,
                           "stop_on_error": True},
               "channel": "shell", "buffers": []}

        q = queue.Queue()
        with self._subs_lock:
            self._subs[msg_id] = q

        out = {"ok": True, "status": "ok", "stdout": "", "stderr": "", "result": "",
               "ename": "", "evalue": "", "traceback": [],
               "execution_count": None, "elapsed": 0.0}
        started = time.time()
        deadline = started + float(timeout)
        got_reply = got_idle = False
        interrupted = False
        try:
            with self._ws_lock:
                if self._ws is None:
                    raise JupyterError("websocket not connected")
                with self._send_lock:
                    self._ws.send_text(json.dumps(msg))

            while True:
                if time.time() >= deadline:
                    if interrupted:
                        break
                    # 一度だけ割り込みを投げ、後片付けの出力を 10 秒だけ待つ
                    interrupted = True
                    out["status"] = "timeout"
                    out["ok"] = False
                    deadline = time.time() + 10.0
                    try:
                        self.interrupt()
                    except JupyterError:
                        pass
                    continue
                try:
                    ev = q.get(timeout=min(max(deadline - time.time(), 0.0), 1.0))
                except queue.Empty:
                    continue
                if ev is None:
                    out["status"] = "abort"
                    out["ok"] = False
                    out["evalue"] = self._ws_error or "websocket closed"
                    break

                mtype = ev.get("msg_type") or (ev.get("header") or {}).get("msg_type")
                content = ev.get("content") or {}
                if mtype == "stream":
                    name = content.get("name", "stdout")
                    text = content.get("text", "")
                    if name == "stderr":
                        out["stderr"] += text
                    else:
                        out["stdout"] += text
                    if on_event and text:
                        on_event({"t": "out", "stream": name, "d": text})
                elif mtype in ("execute_result", "display_data"):
                    text = (content.get("data") or {}).get("text/plain", "")
                    if text:
                        out["result"] += text if not out["result"] else "\n" + text
                        if on_event:
                            on_event({"t": "result", "d": text})
                elif mtype == "error":
                    out["ename"] = content.get("ename", "")
                    out["evalue"] = content.get("evalue", "")
                    out["traceback"] = content.get("traceback", [])
                    if out["status"] == "ok":
                        out["status"] = "error"
                        out["ok"] = False
                    if on_event:
                        on_event({"t": "error", "ename": out["ename"],
                                  "evalue": out["evalue"],
                                  "traceback": out["traceback"]})
                elif mtype == "execute_reply":
                    got_reply = True
                    out["execution_count"] = content.get("execution_count")
                    if content.get("status") == "error" and out["status"] == "ok":
                        out["status"] = "error"
                        out["ok"] = False
                        out["ename"] = content.get("ename", out["ename"])
                        out["evalue"] = content.get("evalue", out["evalue"])
                        out["traceback"] = content.get("traceback", out["traceback"])
                elif mtype == "status":
                    if content.get("execution_state") == "idle":
                        got_idle = True
                if got_reply and got_idle:
                    break
        finally:
            with self._subs_lock:
                self._subs.pop(msg_id, None)
        out["elapsed"] = round(time.time() - started, 3)
        return out

    def execute_json(self, code, timeout=120.0):
        """最後の 1 行に JSON を print するコードを実行し、その JSON を返す。

        カーネル側ヘルパ（kbjob など）の呼び出しに使う共通経路。
        """
        r = self.execute(code, timeout=timeout)
        if r["status"] != "ok":
            raise JupyterError(r["evalue"] or r["stderr"] or
                               "kernel returned status=%s" % r["status"])
        for line in reversed((r["stdout"] or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{") or line.startswith("["):
                try:
                    return json.loads(line)
                except ValueError:
                    continue
        raise JupyterError("no JSON on stdout: %r" % (r["stdout"][-400:],))

    # -------------------------------------------------------------- contents
    def _contents_url(self, path, **q):
        path = (path or "").strip("/")
        if path:
            return self._url("api", "contents", quote(path, safe="/"), **q)
        return self._url("api", "contents", **q)

    def ls(self, path=""):
        r = self._request("GET", self._contents_url(path, content=1))
        entries = []
        for e in (r or {}).get("content", []) or []:
            entries.append({"name": e.get("name"), "path": e.get("path"),
                            "type": e.get("type"), "size": e.get("size")})
        entries.sort(key=lambda e: (e["type"] != "directory", e["name"] or ""))
        return {"path": (path or "").strip("/"), "entries": entries}

    def get_file(self, path):
        r = self._request("GET", self._contents_url(path, content=1, format="base64"))
        if not r or r.get("type") == "directory":
            raise JupyterError("not a file: %s" % path)
        return base64.b64decode(r.get("content") or "")

    def put_file(self, path, data, timeout=300.0):
        parent = os.path.dirname(path)
        if parent:
            self.mkdirs(parent)
        body = {"type": "file", "format": "base64",
                "content": base64.b64encode(data).decode("ascii")}
        self._request("PUT", self._contents_url(path), body=body, timeout=timeout)
        return len(data)

    def mkdir(self, path):
        path = (path or "").strip("/")
        if path:
            self._request("PUT", self._contents_url(path), body={"type": "directory"})

    def mkdirs(self, path):
        acc = []
        for seg in (path or "").strip("/").split("/"):
            if not seg:
                continue
            acc.append(seg)
            try:
                self.mkdir("/".join(acc))
            except JupyterError:
                pass  # 既にある

    def rm(self, path):
        self._request("DELETE", self._contents_url(path))
