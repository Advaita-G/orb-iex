import json
import os
import queue
import socket
import struct
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from fakes import FakeResponse, FakeTransport
from helpers import Harness, calendar, et, settings
from orb_iex.http import ReadOnlyClient
from orb_iex.journal import Journal
from orb_iex.marketdata import BarCache, DataPolicyError, MarketData
from orb_iex.orders import from_trade_update
from orb_iex.secrets import Credentials
from orb_iex.stream import TradeUpdateStream, WebSocket, WebSocketError
from orb_iex.universe import eligible_assets, prefilter
from helpers import bar

CREDS = Credentials("PKFAKEKEYID1111", "fake-secret-value-222")


# --- websocket framing ---------------------------------------------------------

def server_frame(opcode, payload: bytes, fin=True):
    head = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    else:
        head += bytes([126]) + struct.pack("!H", n)
    return head + payload


def read_client_frame(sock):
    b1, b2 = sock.recv(2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack("!H", sock.recv(2))[0]
    mask = sock.recv(4)
    data = b""
    while len(data) < n:
        data += sock.recv(n - len(data))
    return b1 & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(data)), bool(b2 & 0x80)


def test_websocket_frames_ping_fragments_and_close():
    a, b = socket.socketpair()
    ws = WebSocket(a)
    ws.send_json({"action": "auth"})
    op, payload, masked = read_client_frame(b)
    assert op == 1 and masked and json.loads(payload) == {"action": "auth"}
    b.sendall(server_frame(0x9, b"hi"))                       # ping -> pong
    b.sendall(server_frame(0x2, b'{"a":', fin=False) + server_frame(0x0, b"1}"))
    assert json.loads(ws.recv_message()) == {"a": 1}
    op, payload, _ = read_client_frame(b)
    assert op == 0xA and payload == b"hi"
    big = json.dumps({"x": "y" * 300}).encode()
    b.sendall(server_frame(0x1, big))
    assert ws.recv_message() == big
    b.sendall(server_frame(0x8, b""))
    with pytest.raises(WebSocketError):
        ws.recv_message()


class ScriptedWS:
    def __init__(self, messages):
        self.sent, self.messages = [], list(messages)

    def send_json(self, obj):
        self.sent.append(obj)

    def recv_message(self):
        if not self.messages:
            raise WebSocketError("closed")
        return json.dumps(self.messages.pop(0)).encode()

    def close(self):
        pass


def test_stream_reconnects_and_emits_updates():
    order = {"id": "o1", "client_order_id": "orbiex-20260924-AAA-entry", "symbol": "AAA", "side": "buy",
             "type": "stop", "qty": "10", "filled_qty": "4", "filled_avg_price": "50", "status": "partially_filled"}
    auth_ok = {"stream": "authorization", "data": {"status": "authorized", "action": "authenticate"}}
    attempts = []

    def connect(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise OSError("network down")
        return ScriptedWS([auth_ok, {"stream": "listening", "data": {"streams": ["trade_updates"]}},
                           {"stream": "trade_updates", "data": {"event": "partial_fill", "order": order}}])

    q = queue.Queue()
    s = TradeUpdateStream(CREDS, q, connect=connect, sleep=lambda x: None)
    s.run(max_sessions=2)
    items = [q.get() for _ in range(q.qsize())]
    assert items[0] == ("status", "disconnected") and items[1] == ("status", "connected")
    upd = items[2]
    assert upd.order_id == "o1" and upd.filled_qty == D(4) and upd.event == "partial_fill"
    assert items[-1] == ("status", "disconnected")


def test_stream_auth_failure_is_retried_not_trusted():
    q = queue.Queue()
    s = TradeUpdateStream(CREDS, q, connect=lambda u: ScriptedWS([{"data": {"status": "unauthorized"}}]),
                          sleep=lambda x: None)
    s.run(max_sessions=1)
    assert q.get() == ("status", "disconnected")


def test_reconnect_triggers_rest_reconcile_of_missed_fill(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50))
    h.drop_events()                                   # lost while the stream was down
    assert h.engine.current_stop(h.trade()) is None
    h.engine.reconcile(h.clock.now(), side_effects=True)   # what the runner does on ("status","connected")
    h.pump()
    assert h.engine.current_stop(h.trade()).qty == D(100)


def test_trade_update_parsing_ignores_junk():
    assert from_trade_update({"stream": "x"}) is None
    assert from_trade_update({"data": {"event": "new"}}) is None


# --- data layer -----------------------------------------------------------------

def md(tmp_path, routes, now=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc), **over):
    t = FakeTransport(routes)
    c = ReadOnlyClient(CREDS, 150, 150, transport=t, clock=lambda: 0.0, sleep=lambda s: None)
    return MarketData(c, settings(tmp_path, **over), BarCache(tmp_path / "cache.sqlite"), lambda: now), t


def test_recent_sip_refused_before_network(tmp_path):
    data, t = md(tmp_path, {})
    now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(DataPolicyError):
        data.fetch_bars(["AAPL"], "1Min", now - timedelta(minutes=30), now - timedelta(minutes=10), "sip", "t")
    with pytest.raises(DataPolicyError):
        data.fetch_bars(["AAPL"], "1Min", now - timedelta(minutes=30), now, "otc", "t")
    assert t.calls == []


def test_batching_and_pagination(tmp_path):
    def bars(params):
        syms = params["symbols"].split(",")
        assert len(syms) <= 200
        if params.get("page_token") is None and syms[0] == "S0000":
            return FakeResponse(200, {"bars": {syms[0]: [{"t": "2026-09-24T13:30:00Z", "o": 10, "h": 11, "l": 9,
                                                          "c": 10, "v": 5}]}, "next_page_token": "p2"})
        return FakeResponse(200, {"bars": {s: [{"t": "2026-09-24T13:31:00Z", "o": 10, "h": 11, "l": 9, "c": 10,
                                                "v": 5}] for s in syms[:2]}, "next_page_token": None})
    data, t = md(tmp_path, {"/v2/stocks/bars": bars})
    syms = [f"S{i:04d}" for i in range(450)]
    out = data.fetch_bars(syms, "1Min", datetime(2026, 9, 24, 13, 30, tzinfo=timezone.utc),
                          datetime(2026, 9, 24, 13, 35, tzinfo=timezone.utc), "iex", "t")
    assert len(t.calls) == 4                          # 3 chunks (200/200/50) + 1 extra page
    assert len(out["S0000"]) == 2 and out["S0000"][0].t < out["S0000"][1].t


def test_bad_bars_dropped_and_counted(tmp_path):
    def bars(params):
        return FakeResponse(200, {"bars": {"A": [{"t": "2026-09-24T13:30:00Z", "o": 10, "h": 8, "l": 9,
                                                  "c": 10, "v": 5}]}})
    data, _ = md(tmp_path, {"/v2/stocks/bars": bars})
    out = data.fetch_bars(["A"], "5Min", datetime(2026, 9, 24, 13, 30, tzinfo=timezone.utc),
                          datetime(2026, 9, 24, 13, 35, tzinfo=timezone.utc), "iex", "t")
    assert out == {} and data.bad_bars == 1


def test_opening_history_cached_one_call_per_session(tmp_path):
    cal = calendar()
    def bars(params):
        return FakeResponse(200, {"bars": {"A": [{"t": params["start"], "o": 10, "h": 11, "l": 9, "c": 10,
                                                  "v": 7}]}})
    data, t = md(tmp_path, {"/v2/stocks/bars": bars})
    sess = cal.sessions_before(date(2026, 9, 24), 5)
    first = data.opening_bars_history(["A", "B"], sess)
    assert len(t.calls) == 5 and len(first["A"]) == 5 and first["B"] == {}
    data.opening_bars_history(["A", "B"], sess)
    assert len(t.calls) == 5                          # cached, including the symbol with no bars
    assert all(c["params"]["feed"] == "iex" and c["params"]["timeframe"] == "5Min" for c in t.calls)


def test_rth_daily_bars_ignore_extended_hours(tmp_path):
    cal = calendar()
    s = cal.session(date(2026, 9, 23))

    def bars(params):
        assert params["feed"] == "sip" and params["timeframe"] == "30Min" and params["adjustment"] == "raw"
        rows = [{"t": "2026-09-23T12:00:00Z", "o": 1, "h": 99, "l": 1, "c": 1, "v": 999},   # 08:00 ET pre-market
                {"t": "2026-09-23T13:30:00Z", "o": 10, "h": 12, "l": 9, "c": 11, "v": 100},
                {"t": "2026-09-23T19:30:00Z", "o": 11, "h": 13, "l": 10, "c": 12, "v": 50},
                {"t": "2026-09-23T20:00:00Z", "o": 12, "h": 50, "l": 12, "c": 12, "v": 777}]  # after hours
        return FakeResponse(200, {"bars": {"A": rows}})
    data, _ = md(tmp_path, {"/v2/stocks/bars": bars})
    b = data.rth_daily_bars(["A"], [s])["A"][s.day]
    assert (b.o, b.h, b.l, b.c, b.v) == (D(10), D(13), D(9), D(12), 150)


# --- universe --------------------------------------------------------------------

def test_prefilter_uses_paper_adv_and_atr_filters():
    today = date(2026, 9, 24)
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(20)]

    def series(h, l, v):
        return {d: bar(et(d, 9, 30), 50, h, l, 50, v) for d in days}
    daily = {"OK": series(51, 49, 2_000_000),          # ATR 2, ADV 2M
             "THIN": series(51, 49, 999_999),          # ADV below 1M
             "QUIET": series("50.25", "49.75", 5_000_000),  # ATR 0.50: not > 0.50
             "EDGE": series("50.26", "49.75", 1_000_000),   # ATR 0.51, ADV exactly 1M
             "NEW": {d: b for d, b in series(51, 49, 2_000_000).items() if d >= days[10]}}  # too short
    rows = prefilter(daily, today)
    assert [r["symbol"] for r in rows] == ["EDGE", "OK"]
    # no price filter on the prior close: a $3 stock with volume and ATR still passes here
    cheap = {d: bar(et(d, 9, 30), 3, 4, "2.9", 3, 2_000_000) for d in days}
    assert [r["symbol"] for r in prefilter({"CHEAP": cheap}, today)] == ["CHEAP"]


def test_eligible_assets():
    assets = [{"symbol": "AAPL", "tradable": True, "exchange": "NASDAQ"},
              {"symbol": "BRK.B", "tradable": True, "exchange": "NYSE"},
              {"symbol": "SPY", "tradable": True, "exchange": "ARCA"},      # ETF: included
              {"symbol": "XYZ", "tradable": True, "exchange": "AMEX"},
              {"symbol": "CBOE1", "tradable": True, "exchange": "BATS"},    # not NYSE/Nasdaq
              {"symbol": "OTCX", "tradable": True, "exchange": "OTC"},
              {"symbol": "NOTR", "tradable": False, "exchange": "NYSE"},
              {"symbol": "ABC.WS", "tradable": True, "exchange": "NYSE"},   # warrant-style symbol
              {"symbol": "BAD/USD", "tradable": True, "exchange": "NYSE"}]
    assert eligible_assets(assets) == ["AAPL", "BRK.B", "SPY", "XYZ"]
