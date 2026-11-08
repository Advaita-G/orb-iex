"""Alpaca paper trade_updates stream, with a small stdlib WebSocket client.

No extra dependency: RFC 6455 client framing over ssl sockets (text/binary,
ping/pong, close, fragmentation; client frames are masked). HTTPS_PROXY is
honoured with an HTTP CONNECT tunnel.

The stream runs in a background thread and only puts OrderUpdate objects and
("status", ...) tuples on a queue. After every (re)connect it puts
("status", "connected"); the runner then reconciles orders over REST so no
update missed during a disconnect is lost. The REST poll also runs on a
timer, so the bot stays correct even if the stream never connects.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import queue
import socket
import ssl
import struct
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

from . import constants as C
from .logging_setup import LOGGER_NAME, log_event
from .orders import from_trade_update
from .safety import SafetyError, assert_paper_trading_url

log = logging.getLogger(LOGGER_NAME)
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketError(ConnectionError):
    pass


def check_stream_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "wss":
        raise SafetyError(f"refusing non-wss stream URL {url}")
    assert_paper_trading_url("https://" + parts.netloc + parts.path)
    return parts.hostname


def _proxy_for(host: str) -> tuple[str, int] | None:
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy:
        return None
    no_proxy = (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").split(",")
    for n in (x.strip().lstrip("*") for x in no_proxy if x.strip()):
        if host == n.lstrip(".") or host.endswith(n if n.startswith(".") else "." + n):
            return None
    p = urlsplit(proxy if "://" in proxy else "http://" + proxy)
    return p.hostname, p.port or 80


class WebSocket:
    def __init__(self, sock):
        self.sock = sock
        self._buf = b""

    @classmethod
    def connect(cls, url: str, timeout: float = 20.0) -> "WebSocket":
        host = check_stream_url(url)
        path = urlsplit(url).path or "/"
        proxy = _proxy_for(host)
        if proxy:
            raw = socket.create_connection(proxy, timeout=timeout)
            raw.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = raw.recv(4096)
                if not chunk:
                    raise WebSocketError("proxy closed during CONNECT")
                resp += chunk
            status_line = resp.split(b"\r\n", 1)[0]
            if b" 200" not in status_line:
                raise WebSocketError(f"proxy CONNECT failed: {status_line!r}")
        else:
            raw = socket.create_connection((host, 443), timeout=timeout)
        ctx = ssl.create_default_context()
        cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
        if cafile and os.path.isfile(cafile):
            ctx.load_verify_locations(cafile)
        sock = ctx.wrap_socket(raw, server_hostname=host)
        ws = cls(sock)
        ws._handshake(host, path)
        return ws

    def _handshake(self, host: str, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("closed during handshake")
            resp += chunk
        head, self._buf = resp.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if " 101" not in lines[0]:
            raise WebSocketError(f"handshake refused: {lines[0]}")
        headers = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
        expect = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expect:
            raise WebSocketError("bad Sec-WebSocket-Accept")

    # -- frames ---------------------------------------------------------------
    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_json(self, obj) -> None:
        self.send_frame(0x1, json.dumps(obj).encode())

    def recv_message(self) -> bytes:
        """Next complete text/binary message. Answers pings. Raises on close."""
        parts: list[bytes] = []
        while True:
            b1, b2 = self._recv_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b2 & 0x80 else None
            payload = self._recv_exact(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:
                self.send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                return b""          # pong: connection is alive
            if opcode == 0x8:
                raise WebSocketError("server closed the stream")
            if opcode in (0x0, 0x1, 0x2):
                parts.append(payload)
                if fin:
                    return b"".join(parts)

    def close(self) -> None:
        try:
            self.send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class TradeUpdateStream:
    def __init__(self, credentials, out: queue.Queue, url: str = C.PAPER_STREAM_URL,
                 connect: Callable[[str], WebSocket] | None = None, sleep=time.sleep,
                 max_backoff: float = 30.0):
        check_stream_url(url)
        self._creds = credentials
        self.out = out
        self.url = url
        self._connect = connect or (lambda u: WebSocket.connect(u))
        self._sleep = sleep
        self._max_backoff = max_backoff
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.connects = 0

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, name="trade-updates", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run(self, max_sessions: int | None = None) -> None:
        backoff = 1.0
        sessions = 0
        while not self._stop.is_set():
            if max_sessions is not None and sessions >= max_sessions:
                return
            sessions += 1
            ws = None
            try:
                ws = self._connect(self.url)
                key, secret = self._creds.secrets()
                ws.send_json({"action": "auth", "key": key, "secret": secret})
                auth = json.loads(ws.recv_message())
                if (auth.get("data") or {}).get("status") != "authorized":
                    raise WebSocketError(f"stream auth failed: {(auth.get('data') or {}).get('status')}")
                ws.send_json({"action": "listen", "data": {"streams": ["trade_updates"]}})
                self.connects += 1
                backoff = 1.0
                self.out.put(("status", "connected"))
                log_event(log, logging.INFO, "stream", "trade_updates stream connected")
                idle = 0
                while not self._stop.is_set():
                    try:
                        raw = ws.recv_message()
                    except socket.timeout:
                        idle += 1
                        if idle >= 3:
                            raise WebSocketError("no reply to pings")
                        ws.send_frame(0x9, b"ka")
                        continue
                    idle = 0
                    if not raw:
                        continue
                    msg = json.loads(raw)
                    if msg.get("stream") == "trade_updates":
                        u = from_trade_update(msg)
                        if u is not None:
                            self.out.put(u)
            except (OSError, ValueError, WebSocketError) as exc:
                self.out.put(("status", "disconnected"))
                log_event(log, logging.WARNING, "stream",
                          f"trade_updates stream down ({type(exc).__name__}: {exc}); reconnect in {backoff:.0f}s")
                if ws:
                    ws.close()
                if self._stop.is_set():
                    return
                self._sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
