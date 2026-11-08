"""Local read-only HTTP server for the journal dashboard.

Standard library only: ``http.server`` and ``socketserver``. No framework, no
network calls out, no writes. The server binds to loopback by default, answers
``GET`` only, and every handler is a read over the journals.

    python -m orb_iex journal-serve                 # http://127.0.0.1:8787
    python -m orb_iex journal-serve --port 9000
    python -m orb_iex journal-serve --open
"""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from .api import Filters, Journal

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
MAX_PAGE_LIMIT = 5000


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "orbiex-journal/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    journal: Journal  # injected by create_server
    read_only: bool = True
    static_dir: Path = STATIC_DIR

    # -- plumbing ------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - http.server API
        if os.environ.get("ORB_IEX_JOURNAL_VERBOSE"):
            super().log_message(fmt, *args)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path.startswith("/api/"):
                self._api(parsed.path, query)
            else:
                self._static(parsed.path)
        except Exception as exc:  # noqa: BLE001 - a bad request must not kill the server
            self._json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def _forbidden(self, method: str) -> None:
        self._json({"error": f"{method} is not allowed: the journal dashboard is read-only",
                    "read_only": True}, status=405)

    do_POST = do_PUT = do_PATCH = do_DELETE = lambda self: self._forbidden(self.command)  # noqa: E731

    # -- responses -----------------------------------------------------------
    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Read-Only", "true")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _static(self, path: str) -> None:
        rel = unquote(path)
        if rel in ("", "/"):
            rel = "/index.html"
        target = (self.static_dir / posixpath.normpath(rel).lstrip("/")).resolve()
        try:
            target.relative_to(self.static_dir.resolve())
        except ValueError:
            self._json({"error": "path outside the static directory"}, status=403)
            return
        if not target.is_file():
            self._json({"error": f"not found: {rel}"}, status=404)
            return
        body = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Read-Only", "true")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # -- api -----------------------------------------------------------------
    def _api(self, path: str, query: dict[str, list[str]]) -> None:
        journal = self.journal

        def one(name: str) -> str | None:
            values = query.get(name) or []
            return (values[0] or None) if values else None

        if path == "/api/meta":
            return self._json(journal.meta())
        if path == "/api/operations":
            from .operations import snapshot
            return self._json(snapshot(journal.sources.data_dir))
        if path == "/api/overview":
            return self._json(journal.overview(as_of=one("as_of")))
        if path == "/api/trades":
            return self._json(journal.trades(Filters.from_query(query)))
        if path.startswith("/api/trades/"):
            trade_id = unquote(path[len("/api/trades/"):])
            payload = journal.trade(trade_id)
            if payload is None:
                return self._json({"error": f"no trade {trade_id!r} in the record"},
                                  status=404)
            return self._json(payload)
        if path == "/api/execution":
            days = query.get("day") or None
            return self._json(journal.execution(days))
        if path == "/api/opportunities":
            return self._json(journal.opportunities(one("day")))
        if path == "/api/analytics":
            filters = Filters.from_query(query)
            return self._json(journal.analytics(filters))
        if path == "/api/comparison":
            return self._json(journal.comparison())
        if path == "/api/calendar":
            return self._json(journal.calendar(one("month")))
        if path == "/api/gapup":
            from .gapup import summary
            return self._json(summary(journal.sources.data_dir))
        if path == "/api/reviews":
            return self._json(journal.reviews(one("day")))
        if path == "/api/health":
            return self._json(journal.health())
        if path == "/api/mistakes":
            return self._json(journal.mistakes())
        if path == "/api/improvement":
            return self._json(journal.improvement())
        if path == "/api/snapshot":
            return self._json(journal.snapshot())
        if path == "/api/days":
            return self._json({"days": journal.day_list()})
        return self._json({"error": f"unknown endpoint {path!r}",
                           "endpoints": ["/api/meta", "/api/overview", "/api/trades",
                                         "/api/trades/<id>", "/api/execution", "/api/opportunities",
                                         "/api/analytics", "/api/comparison", "/api/calendar",
                                         "/api/reviews", "/api/gapup", "/api/mistakes", "/api/improvement", "/api/health", "/api/snapshot", "/api/days"]},
                          status=404)


def create_server(journal: Journal, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                  *, static_dir: Path | None = None) -> ThreadingHTTPServer:
    """Build (but do not start) the server. ``static_dir`` is overridable for tests."""
    handler = type("BoundDashboardHandler", (DashboardHandler,),
                   {"journal": journal, "static_dir": Path(static_dir) if static_dir else STATIC_DIR})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(journal: Journal, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *,
          open_browser: bool = False, printer: Callable[[str], None] = print) -> None:
    server = create_server(journal, host, port)
    url = f"http://{host}:{server.server_address[1]}/"
    printer(f"ORB-IEX journal dashboard (read-only) on {url}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        printer(f"WARNING: bound to {host}, which is not loopback. The journal is read-only but it "
                "contains your account's trading record; prefer 127.0.0.1.")
    printer("Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        printer("stopping")
    finally:
        server.server_close()
