"""Market data layer: free Alpaca data only, cached on disk.

Feeds:
- SIP (history_feed) only for data that ended at least history_min_age_minutes
  ago. A request that would touch recent SIP is refused here, before any
  network call, because the Basic plan does not include it.
- IEX (opening_feed) for the 09:30-09:35 opening bar, today and in history,
  and for 1-minute bars used by the shadow fill simulator.

All bar requests are multi-symbol, chunked to max_symbols_per_request, paged
with next_page_token, and pass the client's rate limiter.

Completed past sessions never change, so they are cached in SQLite
(runtime/cache.sqlite). A "fetched" marker records which (symbol, day) pairs
were asked for, so a symbol with no trades is not asked for again.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable

from .calendar import NY, MarketCalendar, Session
from .config import Settings
from .logging_setup import LOGGER_NAME, log_event

log = logging.getLogger(LOGGER_NAME)


class DataPolicyError(RuntimeError):
    """A request would need paid data (for example SIP newer than 15 minutes)."""


@dataclass(frozen=True)
class Bar:
    t: datetime       # bar start, UTC
    o: Decimal
    h: Decimal
    l: Decimal
    c: Decimal
    v: int

    def as_dict(self) -> dict:
        return {"t": self.t.isoformat(), "o": str(self.o), "h": str(self.h),
                "l": str(self.l), "c": str(self.c), "v": self.v}


@dataclass(frozen=True)
class Quote:
    """Best bid/ask from the free IEX feed. A side of None means 'no usable price'
    (Alpaca sends 0 when that side of the IEX book is empty)."""
    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    t: datetime | None = None      # the quote's own exchange timestamp (UTC); None = the feed gave none
    feed: str = "iex"
    bid_size: int | None = None    # Alpaca round-lot counts, NOT shares
    ask_size: int | None = None


def parse_quote(symbol: str, raw: dict) -> Quote | None:
    try:
        def side(v):
            d = Decimal(str(v)) if v not in (None, "") else None
            return d if d is not None and d.is_finite() and d > 0 else None
        def size(v):
            if isinstance(v, bool) or v is None:
                return None
            try:
                d = Decimal(str(v))
            except InvalidOperation:
                return None
            return int(d) if d.is_finite() and d >= 0 and d == d.to_integral_value() else None
        t = datetime.fromisoformat(str(raw["t"]).replace("Z", "+00:00")) if raw.get("t") else None
        if t is not None and t.tzinfo is None:
            return None
        return Quote(symbol, side(raw.get("bp")), side(raw.get("ap")), t,
                     bid_size=size(raw.get("bs")), ask_size=size(raw.get("as")))
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_bar(raw: dict) -> Bar | None:
    """Parse and validate one Alpaca bar. Bad data returns None (caller logs)."""
    try:
        t = datetime.fromisoformat(str(raw["t"]).replace("Z", "+00:00"))
        o, h, l, c = (Decimal(str(raw[k])) for k in ("o", "h", "l", "c"))
        v_raw = raw["v"]
        v = int(v_raw)
        if isinstance(v_raw, bool) or v != Decimal(str(v_raw)):
            return None
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None
    if t.tzinfo is None:
        return None
    if not all(x.is_finite() and x > 0 for x in (o, h, l, c)):
        return None
    if h < max(o, c, l) or l > min(o, c) or v < 0:
        return None
    return Bar(t.astimezone(timezone.utc), o, h, l, c, v)


def aggregate(bars: list[Bar]) -> Bar | None:
    if not bars:
        return None
    bars = sorted(bars, key=lambda b: b.t)
    return Bar(bars[0].t, bars[0].o, max(b.h for b in bars), min(b.l for b in bars),
               bars[-1].c, sum(b.v for b in bars))


class BarCache:
    """SQLite cache of completed bars. kind: rth_daily, day_1d, opening_iex."""

    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS bars(kind TEXT, day TEXT, symbol TEXT, t TEXT,
                o TEXT, h TEXT, l TEXT, c TEXT, v INTEGER, PRIMARY KEY(kind, day, symbol));
            CREATE TABLE IF NOT EXISTS fetched(kind TEXT, day TEXT, symbol TEXT,
                PRIMARY KEY(kind, day, symbol));
            CREATE TABLE IF NOT EXISTS assets(day TEXT PRIMARY KEY, body TEXT);
        """)

    def missing(self, kind: str, days: Iterable[date], symbols: Iterable[str]) -> dict[date, set[str]]:
        out: dict[date, set[str]] = {}
        syms = set(symbols)
        for d in days:
            have = {r[0] for r in self.db.execute(
                "SELECT symbol FROM fetched WHERE kind=? AND day=?", (kind, d.isoformat()))}
            need = syms - have
            if need:
                out[d] = need
        return out

    def store(self, kind: str, day: date, symbols: Iterable[str], bars: dict[str, Bar]) -> None:
        self.db.execute("BEGIN")
        try:
            for s, b in bars.items():
                self.db.execute("INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?,?)",
                                (kind, day.isoformat(), s, b.t.isoformat(), str(b.o), str(b.h),
                                 str(b.l), str(b.c), b.v))
            self.db.executemany("INSERT OR IGNORE INTO fetched VALUES(?,?,?)",
                                [(kind, day.isoformat(), s) for s in symbols])
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def load(self, kind: str, days: Iterable[date], symbols: Iterable[str]) -> dict[str, dict[date, Bar]]:
        syms = set(symbols)
        out: dict[str, dict[date, Bar]] = {s: {} for s in syms}
        for d in days:
            for row in self.db.execute(
                    "SELECT symbol,t,o,h,l,c,v FROM bars WHERE kind=? AND day=?", (kind, d.isoformat())):
                if row[0] in syms:
                    out[row[0]][d] = Bar(datetime.fromisoformat(row[1]), Decimal(row[2]),
                                         Decimal(row[3]), Decimal(row[4]), Decimal(row[5]), row[6])
        return out


class MarketData:
    def __init__(self, client, settings: Settings, cache: BarCache,
                 wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.client = client
        self.settings = settings
        self.cache = cache
        self.wall_clock = wall_clock   # real time: data-plan rules use real age
        self.on_progress: Callable[[], None] | None = None
        self.bad_bars = 0
        self._quote_memo: dict[tuple, Quote | None] = {}   # replay only: (symbol, now) / ('sip', symbol, now) -> quote

    # -- policy -------------------------------------------------------------
    def sip_cutoff(self) -> datetime:
        return self.wall_clock() - timedelta(minutes=self.settings.history_min_age_minutes)

    def _check_policy(self, feed: str, end: datetime) -> None:
        if feed not in ("iex", "sip"):
            raise DataPolicyError(f"feed {feed!r} is not a free Alpaca feed")
        if feed == "sip" and end > self.sip_cutoff():
            raise DataPolicyError(
                f"SIP request ending {_iso(end)} is newer than {self.settings.history_min_age_minutes} "
                "minutes; the free Basic plan does not allow it")

    # -- raw multi-symbol bars ------------------------------------------------
    def fetch_bars(self, symbols: list[str], timeframe: str, start: datetime, end: datetime,
                   feed: str, purpose: str) -> dict[str, list[Bar]]:
        self._check_policy(feed, end)
        out: dict[str, list[Bar]] = {}
        size = self.settings.max_symbols_per_request
        uniq = sorted(set(symbols))
        for i in range(0, len(uniq), size):
            chunk = uniq[i:i + size]
            token = None
            while True:
                params = {"symbols": ",".join(chunk), "timeframe": timeframe,
                          "start": _iso(start), "end": _iso(end), "feed": feed,
                          "adjustment": self.settings.adjustment, "limit": 10000}
                if token:
                    params["page_token"] = token
                resp = self.client.data_get("/v2/stocks/bars", params=params, purpose=purpose)
                if self.on_progress:
                    self.on_progress()
                if resp.status == 403 and feed == "sip":
                    raise DataPolicyError(f"Alpaca refused SIP data ({purpose}): {resp.text[:200]}")
                if not resp.ok or not isinstance(resp.data, dict):
                    raise ConnectionError(f"bars request failed ({purpose}): HTTP {resp.status} {resp.text[:200]}")
                for sym, raw_bars in (resp.data.get("bars") or {}).items():
                    for raw in raw_bars or []:
                        bar = parse_bar(raw)
                        if bar is None:
                            self.bad_bars += 1
                            log_event(log, logging.WARNING, "bad_bar",
                                      f"dropped invalid bar for {sym} ({purpose}): {raw}",
                                      symbol=sym, purpose=purpose, raw=raw)
                            continue
                        out.setdefault(sym, []).append(bar)
                token = resp.data.get("next_page_token")
                if not token:
                    break
        for bars in out.values():
            bars.sort(key=lambda b: b.t)
        return out

    # -- regular-session daily bars from SIP 30-minute bars ----------------------
    def corporate_action_snapshot(self, symbols: list[str], day: date) -> dict:
        """Capture currently available metadata, never reconstruct historical knowledge.

        The API filters process_date, not ex_date. Query its entire date domain
        and paginate, then filter effective dates in history_safety. Future
        effective actions are ignored, even though their announcements exist now.
        No snapshot today may authorize a past or future session's scan.
        """
        out = {'observed_at': self.wall_clock().isoformat(), 'symbols': sorted(set(symbols)),
               'actions': [], 'complete': False}
        if day != self.wall_clock().astimezone(NY).date():
            out['error'] = 'historical/future session requires an original decision-time snapshot'
            return out
        try:
            size = self.settings.max_symbols_per_request
            for i in range(0, len(out['symbols']), size):
                token, seen = None, set()
                for _ in range(1000):
                    params = {'symbols': ','.join(out['symbols'][i:i + size]),
                              'start': '0001-01-01', 'end': '9999-12-31',
                              'data_quality': 'all', 'limit': 1000}
                    if token: params['page_token'] = token
                    resp = self.client.data_get('/v1/corporate-actions', params=params,
                                                purpose='history:corporate_actions')
                    if not resp.ok or not isinstance(resp.data, dict):
                        raise DataPolicyError(f'action metadata HTTP {resp.status}')
                    groups = resp.data.get('corporate_actions')
                    if not isinstance(groups, dict): raise DataPolicyError('malformed action groups')
                    for kind, records in groups.items():
                        if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
                            raise DataPolicyError('malformed action records')
                        out['actions'].extend({**r, 'kind': kind} for r in records)
                    token = resp.data.get('next_page_token')
                    if not token: break
                    if not isinstance(token, str) or token in seen:
                        raise DataPolicyError('repeated/invalid corporate action page token')
                    seen.add(token)
                else:
                    raise DataPolicyError('corporate action page limit reached')
            out['complete'] = True
        except Exception as exc:
            out['error'] = f'{type(exc).__name__}: {exc}'
        out['observed_at'] = self.wall_clock().isoformat()
        return out

    def rth_daily_bars(self, symbols: list[str], sessions: list[Session]) -> dict[str, dict[date, Bar]]:
        """Daily bars for 09:30-close only (QC's daily consolidator ignores
        extended hours). Built from SIP 30Min bars, which align to :00/:30."""
        return self._cached("rth_daily", symbols, sessions, self._fetch_rth)

    def _fetch_rth(self, symbols, sessions):
        bars = self.fetch_bars(symbols, "30Min", sessions[0].open, sessions[-1].close,
                               self.settings.history_feed, "history:rth_daily")
        by_day: dict[date, dict[str, Bar]] = {s.day: {} for s in sessions}
        sess = {s.day: s for s in sessions}
        for sym, blist in bars.items():
            groups: dict[date, list[Bar]] = {}
            for b in blist:
                d = b.t.astimezone(NY).date()
                s = sess.get(d)
                if s and s.open <= b.t < s.close:
                    groups.setdefault(d, []).append(b)
            for d, g in groups.items():
                by_day[d][sym] = aggregate(g)
        return by_day

    # -- IEX opening bars (09:30-09:35) -------------------------------------------
    def opening_bars_history(self, symbols: list[str], sessions: list[Session]) -> dict[str, dict[date, Bar]]:
        return self._cached("opening_iex", symbols, sessions, self._fetch_opening_many)

    def _fetch_opening_many(self, symbols, sessions):
        return {s.day: self.opening_bars(symbols, s, purpose="history:opening_iex") for s in sessions}

    def opening_bars(self, symbols: list[str], session: Session, purpose: str = "scan:opening_iex") -> dict[str, Bar]:
        """The first 5-minute IEX bar of the session, one request for all symbols."""
        from . import constants as C
        start = session.open
        end = start + timedelta(minutes=C.OPENING_RANGE_MINUTES)
        bars = self.fetch_bars(symbols, f"{C.OPENING_RANGE_MINUTES}Min", start, end,
                               self.settings.opening_feed, purpose)
        out = {}
        for sym, blist in bars.items():
            for b in blist:
                if b.t == start.astimezone(timezone.utc):
                    out[sym] = b
        return out

    # -- quotes (IEX, free) ----------------------------------------------------
    QUOTE_LIVE_WINDOW_SECONDS = 120        # `now` within this of the wall clock = live
    QUOTE_LOOKBACK_MINUTES = 10            # historical lookup window for replays

    def entry_quotes(self, symbols: list[str], now: datetime) -> dict[str, Quote]:
        """Fresh IEX quote for each symbol as of `now`. Live: the latest quote.
        Replay (fake clock far from real time): the last quote at or before `now`.
        A symbol with no quote is simply absent. Free IEX feed only."""
        wall = self.wall_clock()
        live = abs((wall - now).total_seconds()) <= self.QUOTE_LIVE_WINDOW_SECONDS
        out: dict[str, Quote] = {}
        if live:
            resp = self.client.data_get("/v2/stocks/quotes/latest",
                                        params={"symbols": ",".join(sorted(set(symbols))), "feed": "iex"},
                                        purpose="entry:quote_latest")
            if not resp.ok or not isinstance(resp.data, dict):
                raise ConnectionError(f"latest quotes failed: HTTP {resp.status} {resp.text[:200]}")
            for sym, raw in (resp.data.get("quotes") or {}).items():
                q = parse_quote(sym, raw or {})
                if q is not None:
                    out[sym] = q
            return out
        for sym in sorted(set(symbols)):            # the multi-symbol history endpoint limits across symbols
            if (sym, now) in self._quote_memo:
                if self._quote_memo[(sym, now)] is not None:
                    out[sym] = self._quote_memo[(sym, now)]
                continue
            resp = self.client.data_get(
                "/v2/stocks/quotes",
                params={"symbols": sym, "feed": "iex", "limit": 1, "sort": "desc",
                        "start": _iso(now - timedelta(minutes=self.QUOTE_LOOKBACK_MINUTES)), "end": _iso(now)},
                purpose="entry:quote_at")
            if not resp.ok or not isinstance(resp.data, dict):
                raise ConnectionError(f"historical quote failed for {sym}: HTTP {resp.status} {resp.text[:200]}")
            rows = (resp.data.get("quotes") or {}).get(sym) or []
            q = parse_quote(sym, rows[0]) if rows else None
            self._quote_memo[(sym, now)] = q
            if q is not None:
                out[sym] = q
        return out

    def nbbo_at(self, symbol: str, now: datetime) -> Quote | None:
        """REPLAY ONLY: the consolidated (SIP) best bid/ask at `now`. On 2026-09-30 Alpaca's 422s for
        crossed stop entries matched this quote in 13 of 13 known cases (IEX bid/ask: 11 of 13). SIP data
        this recent is not free, so it is refused unless `now` is older than the SIP cutoff."""
        if now > self.sip_cutoff():
            return None
        key = ("sip", symbol, now)
        if key in self._quote_memo:
            return self._quote_memo[key]
        resp = self.client.data_get(
            "/v2/stocks/quotes",
            params={"symbols": symbol, "feed": "sip", "limit": 1, "sort": "desc",
                    "start": _iso(now - timedelta(minutes=self.QUOTE_LOOKBACK_MINUTES)), "end": _iso(now)},
            purpose="replay:nbbo_at")
        if not resp.ok or not isinstance(resp.data, dict):
            raise ConnectionError(f"historical SIP quote failed for {symbol}: HTTP {resp.status} {resp.text[:200]}")
        rows = (resp.data.get("quotes") or {}).get(symbol) or []
        q = parse_quote(symbol, rows[0]) if rows else None
        self._quote_memo[key] = q
        return q

    def minute_bars_iex(self, symbols: list[str], start: datetime, end: datetime) -> dict[str, list[Bar]]:
        return self.fetch_bars(symbols, "1Min", start, end, "iex", "shadow:minute_iex")

    # -- cache plumbing ----------------------------------------------------------
    def _cached(self, kind, symbols, sessions, fetch) -> dict[str, dict[date, Bar]]:
        wall = self.wall_clock()
        # Only completed sessions are cached (their data cannot change any more).
        done = [s for s in sessions if s.close <= wall]
        missing = self.cache.missing(kind, [s.day for s in done], symbols)
        if missing:
            need_syms = sorted(set().union(*missing.values()))
            need_sessions = [s for s in done if s.day in missing]
            if kind == "opening_iex":
                # one request per session (tiny); fetch only days that are missing
                for s in need_sessions:
                    fetched = fetch(sorted(missing[s.day]), [s])
                    self.cache.store(kind, s.day, missing[s.day], fetched[s.day])
            else:
                # chunk by symbols so thousands of names never sit in memory at once
                size = self.settings.max_symbols_per_request
                for i in range(0, len(need_syms), size):
                    chunk = need_syms[i:i + size]
                    fetched = fetch(chunk, need_sessions)
                    for s in need_sessions:
                        asked = missing[s.day] & set(chunk)
                        if asked:
                            self.cache.store(kind, s.day, asked,
                                             {k: v for k, v in fetched.get(s.day, {}).items() if k in asked})
        return self.cache.load(kind, [s.day for s in done], symbols)

    # -- assets -------------------------------------------------------------
    def assets(self, day: date) -> list[dict]:
        import json
        row = self.cache.db.execute("SELECT body FROM assets WHERE day=?", (day.isoformat(),)).fetchone()
        if row:
            return json.loads(row[0])
        resp = self.client.trading_get("/v2/assets", params={"status": "active", "asset_class": "us_equity"},
                                       purpose="assets")
        if not resp.ok or not isinstance(resp.data, list):
            raise ConnectionError(f"/v2/assets failed: HTTP {resp.status}")
        slim = [{k: a.get(k) for k in ("symbol", "tradable", "shortable", "easy_to_borrow",
                                       "exchange", "status", "class", "fractionable")}
                for a in resp.data]
        self.cache.db.execute("INSERT OR REPLACE INTO assets VALUES(?,?)", (day.isoformat(), json.dumps(slim)))
        return slim


def load_calendar(client, today: date, back_days: int = 90, fwd_days: int = 20) -> MarketCalendar:
    resp = client.trading_get("/v2/calendar", params={
        "start": (today - timedelta(days=back_days)).isoformat(),
        "end": (today + timedelta(days=fwd_days)).isoformat()}, purpose="calendar")
    if not resp.ok or not isinstance(resp.data, list) or not resp.data:
        raise ConnectionError(f"/v2/calendar failed: HTTP {resp.status}")
    return MarketCalendar.from_alpaca(resp.data)
