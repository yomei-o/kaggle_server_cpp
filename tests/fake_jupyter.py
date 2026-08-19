"""Kaggle の Jupyter Server を名乗る偽サーバ（試験用）。

なぜ要るか: kbridge の中身のほとんどは「Jupyter プロトコルを正しく喋れているか」で、
それを確かめるのに毎回 Kaggle のセッションを起こすのは遅いうえ GPU 無料枠を削る。
このサーバは Kaggle と**同じ URL 形（/k/<id>/<token>/proxy）・同じ REST・同じ
カーネル WebSocket** を喋るので、python 版と cpp 版の両方をローカルで試験でき、
パリティ試験（tests/parity.py）の土台にもなる。

本物ではない点:
  * 認証はトークンの一致を見るだけ
  * カーネルはこのプロセス内の Python 名前空間（別プロセスではない）
  * GPU は無い（/gpu は gpus:[] を返すのが正しい応答になる）

    python tests/fake_jupyter.py --port 8899
    # -> URL: http://127.0.0.1:8899/k/fake/TESTTOKEN/proxy
"""

import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PROTOCOL_VERSION = "5.3"

KERNEL_ID = "fake-kernel-0001"
TOKEN = "TESTTOKEN"
PREFIX = "/k/fake/%s/proxy" % TOKEN


# --------------------------------------------------------------------- kernel

class Kernel:
    """1 個の Python 名前空間。execute_request を順に処理する。"""

    def __init__(self):
        self.id = KERNEL_ID
        self.ns = {"__name__": "__main__"}
        self.count = 0
        self.lock = threading.Lock()
        self.thread = None
        self.running = False   # セル実行中だけ True
        self.interrupts = 0    # 受けた割り込み要求の数（効かせはしない）
        self._flag_lock = threading.Lock()

    def new_id(self):
        self.id = "fake-kernel-" + uuid.uuid4().hex[:8]
        return self.id

    def interrupt(self):
        """割り込み要求を受け付ける（記録するだけ）。

        当初は ctypes で実行スレッドへ KeyboardInterrupt を投げていたが、
        非同期例外は次のバイトコード境界でしか上がらないため time.sleep の途中では
        効かず、セルが終わったあとに迷子の例外として飛んできて試験プロセスごと
        落とすことがあった。本物のカーネルも C の中で止まっていれば割り込みは
        すぐには効かないので、ここでは**効かない割り込み**を正として記録だけする。

        呼ぶ側（kbridge）にとって大事なのは「割り込みを投げたあと status=timeout で
        返ること」で、それはこれで十分試験できる。試験では sleep を短くしておき、
        カーネルが自分で空くのを待つ。
        """
        with self._flag_lock:
            self.interrupts += 1


class _StreamOut(io.TextIOBase):
    """print を書いた瞬間に stream メッセージへ変える。"""

    def __init__(self, emit, name):
        self.emit = emit
        self.name = name

    def write(self, s):
        if s:
            self.emit(self.name, s)
        return len(s)

    def flush(self):
        pass


# ------------------------------------------------------------------ websocket

def ws_send(conn, payload, opcode=0x1):
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    n = len(data)
    head = bytearray([0x80 | opcode])
    if n < 126:
        head.append(n)
    elif n < 65536:
        head.append(126)
        head += struct.pack(">H", n)
    else:
        head.append(127)
        head += struct.pack(">Q", n)
    conn.sendall(bytes(head) + data)   # サーバ -> クライアントはマスクしない


def ws_recv(conn, buf):
    """1 フレーム読む。戻り値 (opcode, payload, buf)。切断なら (None, None, buf)。"""
    def need(n):
        nonlocal buf
        while len(buf) < n:
            chunk = conn.recv(65536)
            if not chunk:
                return False
            buf += chunk
        return True

    if not need(2):
        return None, None, buf
    b0, b1 = buf[0], buf[1]
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    n = b1 & 0x7F
    off = 2
    if n == 126:
        if not need(off + 2):
            return None, None, buf
        n = struct.unpack(">H", buf[off:off + 2])[0]
        off += 2
    elif n == 127:
        if not need(off + 8):
            return None, None, buf
        n = struct.unpack(">Q", buf[off:off + 8])[0]
        off += 8
    mask = b""
    if masked:
        if not need(off + 4):
            return None, None, buf
        mask = buf[off:off + 4]
        off += 4
    if not need(off + n):
        return None, None, buf
    payload = bytearray(buf[off:off + n])
    if masked:
        for i in range(n):
            payload[i] ^= mask[i & 3]
    buf = buf[off + n:]
    return opcode, bytes(payload), buf


# -------------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeJupyter/0.1"

    # ---- 共通
    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("[fake] " + fmt % args + "\n")

    def _rel(self):
        p = urlparse(self.path)
        path = p.path
        if not path.startswith(PREFIX):
            return None, {}
        return path[len(PREFIX):] or "/", parse_qs(p.query)

    def _auth_ok(self, query):
        if query.get("token", [None])[0] == TOKEN:
            return True
        if self.headers.get("Authorization") == "token " + TOKEN:
            return True
        return False

    def _json(self, obj, status=200, extra_headers=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    # ---- contents API（ファイルは server.root の下の実ファイル）
    def _fs(self, rel):
        rel = unquote(rel).strip("/")
        full = os.path.normpath(os.path.join(self.server.root, rel))
        if not full.startswith(os.path.normpath(self.server.root)):
            return None, None
        return rel, full

    def _contents_get(self, rel, query):
        rel, full = self._fs(rel)
        if full is None or not os.path.exists(full):
            return self._json({"message": "no such file: %s" % rel}, 404)
        name = os.path.basename(full) or ""
        if os.path.isdir(full):
            content = []
            for fn in sorted(os.listdir(full)):
                p = os.path.join(full, fn)
                content.append({
                    "name": fn, "path": (rel + "/" + fn).strip("/"),
                    "type": "directory" if os.path.isdir(p) else "file",
                    "size": None if os.path.isdir(p) else os.path.getsize(p)})
            return self._json({"name": name, "path": rel, "type": "directory",
                               "content": content, "format": "json"})
        with open(full, "rb") as f:
            data = f.read()
        fmt = query.get("format", ["text"])[0]
        if fmt == "base64":
            payload = base64.b64encode(data).decode("ascii")
        else:
            payload = data.decode("utf-8", "replace")
        return self._json({"name": name, "path": rel, "type": "file",
                           "format": fmt, "size": len(data), "content": payload})

    def _contents_put(self, rel):
        body = self._body()
        rel, full = self._fs(rel)
        if full is None:
            return self._json({"message": "bad path"}, 400)
        if body.get("type") == "directory":
            os.makedirs(full, exist_ok=True)
            return self._json({"name": os.path.basename(full), "path": rel,
                               "type": "directory"}, 201)
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        if body.get("format") == "base64":
            data = base64.b64decode(body.get("content") or "")
        else:
            data = (body.get("content") or "").encode("utf-8")
        with open(full, "wb") as f:
            f.write(data)
        return self._json({"name": os.path.basename(full), "path": rel,
                           "type": "file", "size": len(data)}, 201)

    # ---- ルーティング
    def do_GET(self):
        rel, query = self._rel()
        if rel is None:
            return self._json({"message": "not found"}, 404)
        if rel == "/":
            # 本物と同じく、まずここで _xsrf クッキーを配る
            return self._json({"ok": True}, 200,
                              {"Set-Cookie": "_xsrf=%s; Path=/" % self.server.xsrf})
        if not self._auth_ok(query):
            return self._json({"message": "forbidden"}, 403)
        if rel == "/api/kernels":
            k = self.server.kernel
            return self._json([{"id": k.id, "name": "python3",
                                "execution_state": "idle", "connections": 1}])
        if rel.startswith("/api/kernels/") and rel.endswith("/channels"):
            return self._websocket()
        if rel.startswith("/api/contents"):
            return self._contents_get(rel[len("/api/contents"):], query)
        return self._json({"message": "not found: %s" % rel}, 404)

    def do_POST(self):
        rel, query = self._rel()
        if rel is None or not self._auth_ok(query):
            return self._json({"message": "forbidden"}, 403)
        k = self.server.kernel
        if rel == "/api/sessions":
            body = self._body()
            return self._json({"id": uuid.uuid4().hex, "path": body.get("path", ""),
                               "name": "", "type": "notebook",
                               "kernel": {"id": k.id, "name": "python3",
                                          "execution_state": "idle"}}, 201)
        if rel.endswith("/interrupt"):
            k.interrupt()
            return self._json({}, 204)
        if rel.endswith("/restart"):
            k.ns.clear()
            k.ns["__name__"] = "__main__"
            return self._json({"id": k.new_id(), "name": "python3",
                               "execution_state": "starting"})
        return self._json({"message": "not found: %s" % rel}, 404)

    def do_PUT(self):
        rel, query = self._rel()
        if rel is None or not self._auth_ok(query):
            return self._json({"message": "forbidden"}, 403)
        if rel.startswith("/api/contents"):
            return self._contents_put(rel[len("/api/contents"):])
        return self._json({"message": "not found"}, 404)

    def do_DELETE(self):
        rel, query = self._rel()
        if rel is None or not self._auth_ok(query):
            return self._json({"message": "forbidden"}, 403)
        if rel.startswith("/api/contents"):
            _, full = self._fs(rel[len("/api/contents"):])
            if full and os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
            elif full and os.path.exists(full):
                os.remove(full)
            return self._json({}, 204)
        if rel.startswith("/api/kernels/"):
            return self._json({}, 204)
        return self._json({"message": "not found"}, 404)

    # ---- カーネルチャンネル（WebSocket）
    def _websocket(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return self._json({"message": "not a websocket request"}, 400)
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        try:
            self.wfile.flush()
        except OSError:
            return

        conn = self.connection
        kernel = self.server.kernel
        send_lock = threading.Lock()
        buf = b""

        def send(msg):
            with send_lock:
                try:
                    ws_send(conn, json.dumps(msg))
                except OSError:
                    pass

        while True:
            try:
                opcode, payload, buf = ws_recv(conn, buf)
            except OSError:
                break
            if opcode is None or opcode == 0x8:      # 切断 / close
                break
            if opcode == 0x9:                        # ping
                with send_lock:
                    try:
                        ws_send(conn, payload, opcode=0xA)
                    except OSError:
                        break
                continue
            if opcode != 0x1:
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except ValueError:
                continue
            if (msg.get("header") or {}).get("msg_type") != "execute_request":
                continue
            try:
                self._execute(kernel, msg, send)
            except BaseException as e:   # 迷子の KeyboardInterrupt を握りつぶす
                if self.server.verbose:
                    sys.stderr.write("[fake] stray %s in handler\n" % type(e).__name__)

        # 本物のサーバと同じく、WebSocket が終わったら接続そのものを閉じる。
        # ここで閉じないと、close フレームを送った側の read がいつまでも返らず、
        # 切断処理が固まる（実際に cpp 版の DELETE /session がそれで詰まった）。
        self.close_connection = True
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass

    def _execute(self, kernel, msg, send):
        parent = msg["header"]
        code = (msg.get("content") or {}).get("code", "")

        def envelope(msg_type, content, channel="iopub"):
            return {"header": {"msg_id": uuid.uuid4().hex, "username": "fake",
                               "session": parent.get("session", ""),
                               "date": time.strftime("%Y-%m-%dT%H:%M:%S.000000Z",
                                                     time.gmtime()),
                               "msg_type": msg_type, "version": PROTOCOL_VERSION},
                    "parent_header": parent, "metadata": {},
                    "content": content, "channel": channel,
                    "msg_type": msg_type, "buffers": []}

        def emit_stream(name, text):
            send(envelope("stream", {"name": name, "text": text}))

        send(envelope("status", {"execution_state": "busy"}))
        with kernel.lock:
            kernel.count += 1
            count = kernel.count
            out = _StreamOut(emit_stream, "stdout")
            err_out = _StreamOut(emit_stream, "stderr")
            old = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = out, err_out
            with kernel._flag_lock:
                kernel.thread = threading.current_thread()
                kernel.running = True
            status = "ok"
            einfo = {}
            try:
                exec(compile(code, "<kbridge-cell>", "exec"), kernel.ns)
            except BaseException as e:                 # KeyboardInterrupt も拾う
                status = "error"
                tb = traceback.format_exception(type(e), e, e.__traceback__)
                einfo = {"ename": type(e).__name__, "evalue": str(e),
                         "traceback": [t.rstrip("\n") for t in tb]}
                send(envelope("error", einfo))
            finally:
                sys.stdout, sys.stderr = old
                with kernel._flag_lock:
                    kernel.running = False
                    kernel.thread = None

        reply = {"status": status, "execution_count": count,
                 "user_expressions": {}, "payload": []}
        reply.update(einfo)
        send(envelope("execute_reply", reply, channel="shell"))
        send(envelope("status", {"execution_state": "idle"}))


class FakeJupyter(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # クライアントが先に切っただけ（ストリーム中断など）は試験のノイズになる
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError)):
            return
        if self.verbose:
            traceback.print_exc()

    def __init__(self, addr, root, verbose=False):
        super().__init__(addr, Handler)
        self.root = root
        self.kernel = Kernel()
        self.xsrf = uuid.uuid4().hex
        self.verbose = verbose


def serve(port=8899, root=None, verbose=False):
    root = root or tempfile.mkdtemp(prefix="fakejup-")
    os.makedirs(root, exist_ok=True)
    srv = FakeJupyter(("127.0.0.1", port), root, verbose)
    url = "http://127.0.0.1:%d%s" % (srv.server_address[1], PREFIX)
    return srv, url, root


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--root", default=None, help="contents のルート（既定は一時ディレクトリ）")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    srv, url, root = serve(a.port, a.root, a.verbose)
    print("fake jupyter on %s" % url)
    print("contents root: %s" % root)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
