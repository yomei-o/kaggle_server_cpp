"""RFC 6455 WebSocket クライアント（標準ライブラリのみ）。

外部依存を増やさないために自前実装している。C++ 版は WinHTTP の WebSocket API を使うが、
上位（kbridge.jupyter / cpp/pure/jupyter.hpp）から見た振る舞いは同じ:
  connect() -> send_text() -> recv(timeout) を繰り返す -> close()

対応範囲: テキスト/バイナリメッセージ、フラグメント連結、ping への自動 pong、close。
拡張 (permessage-deflate) は要求しない。サブプロトコルも要求しない
（v1.kernel.websocket.jupyter.org を要求するとバイナリ多重化形式になるため、あえて使わない）。
"""

import base64
import hashlib
import os
import socket
import ssl
import struct
from urllib.parse import urlparse

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(Exception):
    pass


class WebSocketClosed(WebSocketError):
    pass


class WebSocketClient:
    def __init__(self, url, headers=None, connect_timeout=30.0, origin=None):
        self.url = url
        self.headers = dict(headers or {})
        self.connect_timeout = connect_timeout
        self.origin = origin
        self.sock = None
        self._buf = b""
        self._closed = False

    # ------------------------------------------------------------------ setup
    def connect(self):
        p = urlparse(self.url)
        secure = p.scheme in ("wss", "https")
        port = p.port or (443 if secure else 80)
        host = p.hostname
        path = p.path or "/"
        if p.query:
            path += "?" + p.query

        raw = socket.create_connection((host, port), timeout=self.connect_timeout)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if secure:
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=host)
        self.sock = raw

        key = base64.b64encode(os.urandom(16)).decode()
        hdrs = {
            "Host": p.netloc,
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": key,
            "Sec-WebSocket-Version": "13",
            "User-Agent": "kbridge/0.1",
        }
        if self.origin:
            hdrs["Origin"] = self.origin
        hdrs.update(self.headers)
        req = "GET %s HTTP/1.1\r\n" % path
        req += "".join("%s: %s\r\n" % (k, v) for k, v in hdrs.items())
        req += "\r\n"
        self.sock.sendall(req.encode("utf-8"))

        head = self._read_until(b"\r\n\r\n", self.connect_timeout)
        text = head.decode("latin-1")
        status_line = text.split("\r\n", 1)[0]
        if " 101 " not in status_line:
            raise WebSocketError("websocket upgrade failed: %s\n%s" % (status_line, text[:800]))
        accept = ""
        for line in text.split("\r\n")[1:]:
            if line.lower().startswith("sec-websocket-accept:"):
                accept = line.split(":", 1)[1].strip()
        want = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        if accept != want:
            raise WebSocketError("bad Sec-WebSocket-Accept")
        return self

    def _read_until(self, marker, timeout):
        self.sock.settimeout(timeout)
        while marker not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            self._buf += chunk
        head, self._buf = self._buf.split(marker, 1)
        return head + marker

    # ------------------------------------------------------------------- send
    def send_text(self, text):
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_pong(self, payload=b""):
        self._send_frame(OP_PONG, payload)

    def _send_frame(self, opcode, payload):
        if self._closed or self.sock is None:
            raise WebSocketClosed("websocket is closed")
        n = len(payload)
        hdr = bytearray()
        hdr.append(0x80 | opcode)  # FIN
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self.sock.sendall(bytes(hdr) + masked)

    # ------------------------------------------------------------------- recv
    def recv(self, timeout=None):
        """1 メッセージ受信。timeout 秒で何も来なければ None（接続は維持）。

        戻り値は str（テキスト）または bytes（バイナリ）。close を受けたら WebSocketClosed。
        """
        parts = []
        first_op = None
        while True:
            fin, opcode, payload = self._recv_frame(timeout)
            if fin is None:
                return None  # timeout
            if opcode == OP_PING:
                self.send_pong(payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self._closed = True
                raise WebSocketClosed("server closed websocket")
            if opcode in (OP_TEXT, OP_BIN):
                first_op = opcode
                parts = [payload]
            elif opcode == OP_CONT:
                parts.append(payload)
            if fin:
                data = b"".join(parts)
                if first_op == OP_TEXT:
                    return data.decode("utf-8", "replace")
                return data

    def _recv_frame(self, timeout):
        try:
            head = self._read_exact(2, timeout)
        except socket.timeout:
            return None, None, None
        if head is None:
            return None, None, None
        b0, b1 = head[0], head[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._read_exact(2, timeout))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read_exact(8, timeout))[0]
        mask = self._read_exact(4, timeout) if masked else None
        payload = self._read_exact(n, timeout) if n else b""
        if mask:
            payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        return fin, opcode, payload

    def _read_exact(self, n, timeout):
        # 一度データが来始めたら、フレーム途中で諦めない（部分読みは破棄不能なため）
        started = len(self._buf) > 0
        while len(self._buf) < n:
            self.sock.settimeout(timeout if not started else max(timeout or 30.0, 30.0))
            chunk = self.sock.recv(65536)
            if not chunk:
                self._closed = True
                raise WebSocketClosed("connection reset")
            started = True
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    # ------------------------------------------------------------------ close
    def close(self):
        if self.sock is None:
            return
        try:
            if not self._closed:
                self._send_frame(OP_CLOSE, struct.pack(">H", 1000))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        self.sock = None
        self._closed = True
