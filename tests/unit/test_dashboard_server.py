"""The read-only HTTP surface, exercised over a real socket.

The dashboard's whole safety claim is that it cannot write. These tests open the real
server on a real port, walk every endpoint, and check that both databases are byte-for-byte
unchanged afterwards and that every write verb is refused.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import dashboard_fixture as fx
import pytest

from orb_iex.dashboard.api import Journal
from orb_iex.dashboard.server import create_server

ENDPOINTS = ("/api/meta", "/api/overview", "/api/trades", "/api/execution", "/api/opportunities",
             "/api/analytics", "/api/comparison", "/api/calendar", "/api/reviews", "/api/mistakes", "/api/improvement", "/api/health",
             "/api/snapshot", "/api/days")


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    base = tmp_path_factory.mktemp("server-root")
    fx.write(base)
    return base


@pytest.fixture(scope="module")
def journal(root):
    return Journal(fx.sources_for(root), fx.settings_for(root))


@pytest.fixture(scope="module")
def port(journal):
    server = create_server(journal, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(port: int, path: str, method: str = "GET"):
    """-> (status, headers, raw body). Raises nothing for 4xx/5xx: they are answers too."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    if method in ("POST", "PUT", "PATCH"):
        req.data = b"{}"
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def get_json(port: int, path: str) -> dict:
    status, headers, body = request(port, path)
    assert status == 200, (path, status, body[:400])
    return json.loads(body)


def raw_request(port: int, request_line: str) -> bytes:
    """Send a hand-built request line, so a path can be tested exactly as written."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(f"{request_line}\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# every endpoint answers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ENDPOINTS)
def test_every_endpoint_answers_with_json(port, path):
    payload = get_json(port, path)
    assert isinstance(payload, dict) and payload
    assert payload.get("error") is None


def test_meta_and_overview_state_the_read_only_contract(port):
    meta = get_json(port, "/api/meta")
    assert meta["read_only"] is True and meta["mode"] == "paper"
    assert meta["accounting_rules"]["missing"].startswith("a missing value is reported")

    overview = get_json(port, "/api/overview?as_of=2026-10-09")
    assert overview["periods"]["all_time"]["trades"] == 4
    assert overview["status"]["mode"] == "paper"
    assert overview["equity"]["points"][0]["day"] == "2026-10-01"


def test_trades_endpoint_filters_and_serves_a_single_trade(port):
    page = get_json(port, "/api/trades?symbol=MSFT&limit=5")
    assert page["total"] == 1 and page["rows"][0]["trade_id"] == "2026-10-06:MSFT"

    quoted = urllib.parse.quote("2026-10-06:MSFT", safe="")
    detail = get_json(port, f"/api/trades/{quoted}")
    assert detail["trade"]["trade_id"] == "2026-10-06:MSFT"
    assert [s["step"] for s in detail["steps"]][:3] == ["signal", "candidate", "quote"]
    assert detail["reconciliation"]["balanced"] is True

    status, _, body = request(port, "/api/trades/2099-01-01:NOPE")
    assert status == 404 and "no trade" in json.loads(body)["error"]


def test_opportunities_calendar_and_reviews_use_the_requested_day(port):
    funnel = get_json(port, "/api/opportunities?day=2026-10-07")
    assert funnel["selected"] == "2026-10-07"
    assert funnel["summary"]["diagnosis"]["state"] == "healthy_no_trade"
    assert "2026-10-09" in funnel["available_days"]

    calendar = get_json(port, "/api/calendar?month=2026-10")["calendar"]
    assert calendar["totals"]["traded"] == 5 and calendar["totals"]["market_closed"] == 9

    assert get_json(port, "/api/reviews?day=2026-10-06")["daily"]["label"] == "2026-10-06"


def test_a_day_with_no_data_is_answered_not_guessed(port):
    funnel = get_json(port, "/api/opportunities?day=2030-01-01")
    assert funnel["scan_ran"] is False
    assert funnel["summary"]["diagnosis"]["state"] == "unavailable"
    assert funnel["rules_source"] == "none"


def test_bad_query_values_never_500(port):
    page = get_json(port, "/api/trades?limit=abc&offset=-9&min_net=nope&sort=not-a-column&defect=maybe")
    assert page["limit"] == 500 and page["offset"] == 0
    # An unparseable filter is dropped rather than filtered on: no invented threshold.
    assert {f["name"] for f in page["filters"]}.isdisjoint({"min_net", "defect", "limit", "offset"})
    status, _, body = request(port, "/api/trades?as_of[]=1")
    assert status == 200 and json.loads(body)["total"] == 4


def test_unknown_endpoint_is_a_404_that_lists_the_real_ones(port):
    status, headers, body = request(port, "/api/does-not-exist")
    payload = json.loads(body)
    assert status == 404 and "unknown endpoint" in payload["error"]
    assert "/api/trades/<id>" in payload["endpoints"]
    assert headers["X-Read-Only"] == "true"


# ---------------------------------------------------------------------------
# read-only, enforced
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_write_verbs_are_refused(port, method):
    status, headers, body = request(port, "/api/trades", method=method)
    payload = json.loads(body)
    assert status == 405 and payload["read_only"] is True
    assert "read-only" in payload["error"]


def test_walking_every_endpoint_leaves_the_journals_byte_identical(port, root):
    state = Path(root) / "runtime" / "journal.sqlite"
    trade = Path(root) / "runtime" / "journal" / "trade_journal.sqlite"
    before = (digest(state), digest(trade))
    for path in ENDPOINTS:
        assert request(port, path)[0] == 200
    for path in ("/api/trades?limit=100", "/api/trades/2026-10-06:MSFT", "/api/opportunities",
                 "/api/calendar?month=2026-10", "/api/reviews?day=2026-10-06"):
        assert request(port, path)[0] == 200
    assert (digest(state), digest(trade)) == before


def test_every_response_advertises_read_only(port):
    for path in ("/api/meta", "/api/trades", "/"):
        status, headers, _ = request(port, path)
        assert status == 200 and headers["X-Read-Only"] == "true"
        assert headers["Cache-Control"] == "no-store"


def test_head_returns_headers_without_a_body(port):
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(b"HEAD /api/meta HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        raw = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            raw += chunk
    head, _, body = raw.partition(b"\r\n\r\n")
    assert b" 200 " in head.split(b"\r\n")[0]
    assert b"X-Read-Only: true" in head
    assert body == b""


# ---------------------------------------------------------------------------
# static assets
# ---------------------------------------------------------------------------

def test_the_shipped_frontend_is_served(port):
    status, headers, body = request(port, "/")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert b"Paper P&amp;L journal" in body and b"<script" in body

    for asset, kind in (("/app.js", "javascript"), ("/styles.css", "text/css")):
        status, headers, body = request(port, asset)
        assert status == 200, asset
        assert kind in headers["Content-Type"] and body


def test_path_traversal_is_refused(port):
    cases = ("/../pyproject.toml", "/%2e%2e/%2e%2e/pyproject.toml", "/..%2f..%2fsettings.toml",
             "/static/../../pyproject.toml")
    for case in cases:
        raw = raw_request(port, f"GET {case} HTTP/1.1")
        head, _, body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ")[1])
        assert status in (403, 404), (case, status)
        assert b"[project]" not in body and b"name = " not in body, case


def test_a_missing_static_file_is_a_404(port):
    status, _, body = request(port, "/nope.js")
    assert status == 404 and "not found" in json.loads(body)["error"]


def test_concurrent_requests_are_served(port):
    results = []

    def worker(path: str):
        status, _, body = request(port, path)
        results.append((status, len(body)))

    threads = [threading.Thread(target=worker, args=(p,)) for p in
               ("/api/overview", "/api/trades", "/api/analytics", "/api/execution", "/api/snapshot")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert len(results) == 5 and all(status == 200 for status, _ in results)


def test_bound_to_localhost_only(journal):
    """The server must not be reachable off the box by default."""
    from orb_iex.dashboard import server as server_mod
    assert server_mod.DEFAULT_HOST == "127.0.0.1"
    try:
        http.client.HTTPConnection("127.0.0.1", 9, timeout=0.1)
    except OSError:
        pass                                                     # nothing listening: that is the point
