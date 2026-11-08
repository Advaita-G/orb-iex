"""Offline fakes: an HTTP transport that answers like Alpaca, no network."""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlsplit


class FakeResponse:
    def __init__(self, status: int, data=None, headers=None, text=""):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.text = text or ("" if data is None else str(data))

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class FakeTransport:
    """Routes GET requests by path. Records every call. Rejects non-GET."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls: list[dict] = []

    def request(self, method, url, params=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append({"method": method, "url": url, "params": dict(params or {}),
                           "headers": dict(headers or {})})
        assert method == "GET", f"non-GET request attempted: {method} {url}"
        parts = urlsplit(url)
        handler = self.routes.get(parts.path)
        if handler is None:
            return FakeResponse(404, {"message": "not found"})
        return handler(params or {}) if callable(handler) else handler


def weekday_sessions(start: date, days: int, early: set[date] = frozenset()):
    out, d = [], start
    while len(out) < days:
        if d.weekday() < 5:
            out.append({"date": d.isoformat(), "open": "09:30",
                        "close": "13:00" if d in early else "16:00"})
        d += timedelta(days=1)
    return out


def basic_plan_routes(account_number="PA1234567890", sip_recent_status=403):
    """Alpaca answers as seen with a Basic (free) plan paper account."""

    def account(_):
        return FakeResponse(200, {"account_number": account_number, "status": "ACTIVE",
                                  "currency": "USD", "equity": "100000", "multiplier": "4",
                                  "shorting_enabled": True, "trading_blocked": False,
                                  "account_blocked": False, "pattern_day_trader": False,
                                  "daytrade_count": 0})

    def clock(_):
        from datetime import datetime, timezone
        return FakeResponse(200, {"timestamp": datetime.now(timezone.utc).isoformat(),
                                  "is_open": False, "next_open": "x", "next_close": "y"})

    def calendar(params):
        start = date.fromisoformat(params["start"])
        return FakeResponse(200, weekday_sessions(start, 18))

    def assets(_):
        from itertools import islice, product
        letters = (("".join(p)) for p in product("BCDEFGHJKL", repeat=4))
        syms = ["SPY", "AAPL", "MSFT"] + list(islice(letters, 2500))
        return FakeResponse(200, [{"symbol": s, "tradable": True, "shortable": True,
                                   "easy_to_borrow": i % 2 == 0, "exchange": "NASDAQ"}
                                  for i, s in enumerate(syms)])

    def latest(params):
        return FakeResponse(200, {"bars": {s: {"t": "2026-09-24T19:59:00Z", "c": 1}
                                           for s in params["symbols"].split(",")}},
                            headers={"X-RateLimit-Limit": "200"})

    def bars(params):
        syms = params["symbols"].split(",")
        if params.get("feed") == "sip" and params.get("timeframe") == "1Min":
            if sip_recent_status == 403:
                return FakeResponse(403, {"message": "subscription does not permit querying recent SIP data"},
                                    text="subscription does not permit querying recent SIP data")
            return FakeResponse(200, {"bars": {"SPY": []}})
        if len(syms) > 2000:
            return FakeResponse(414, None, text="URI Too Long")
        n = 20 if params.get("timeframe") == "1Day" and len(syms) <= 5 else 1
        bar = {"t": "2026-09-23T13:30:00Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000, "n": 12}
        return FakeResponse(200, {"bars": {s: [bar] * n for s in syms}, "next_page_token": None},
                            headers={"X-RateLimit-Limit": "200"})

    return {
        "/v2/account": account, "/v2/clock": clock, "/v2/calendar": calendar,
        "/v2/assets": assets, "/v2/stocks/bars/latest": latest, "/v2/stocks/bars": bars,
    }


class FakeTradingTransport(FakeTransport):
    """Like FakeTransport but also answers order writes (POST/PATCH/DELETE)."""

    def __init__(self, routes=None, writes=None):
        super().__init__(routes)
        self.writes = writes or {}

    def request(self, method, url, params=None, headers=None, timeout=None, allow_redirects=True, json=None):
        if method == "GET":
            return super().request(method, url, params=params, headers=headers, timeout=timeout,
                                   allow_redirects=allow_redirects)
        self.calls.append({"method": method, "url": url, "json": json, "headers": dict(headers or {})})
        path = urlsplit(url).path
        handler = self.writes.get((method, path)) or self.writes.get((method, "*"))
        if handler is None:
            return FakeResponse(404, {"message": "not found"})
        return handler(json) if callable(handler) else handler
