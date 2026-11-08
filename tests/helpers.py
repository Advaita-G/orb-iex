"""Offline building blocks for engine/runner tests: sessions, synthetic bars,
a fake data layer and an engine wired to the local simulator."""

from __future__ import annotations

import queue
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from orb_iex.calendar import NY, MarketCalendar, Schedule, Session
from orb_iex.clock import FakeClock
from orb_iex.config import parse_settings
from orb_iex.engine import Engine
from orb_iex.journal import Journal
from orb_iex.marketdata import Bar, Quote
from orb_iex.sim import SimBroker
from orb_iex.strategy import EntryPlan

D = Decimal


def et(day: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), tzinfo=NY)


def sessions(start: date, end: date, holidays=(), early=()) -> list[Session]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in holidays:
            out.append(Session(d, et(d, 9, 30), et(d, 13, 0) if d in early else et(d, 16, 0)))
        d += timedelta(days=1)
    return out


def calendar(start=date(2026, 7, 1), end=date(2026, 10, 30), holidays=(date(2026, 9, 7),), early=()):
    return MarketCalendar(sessions(start, end, holidays, early))


def bar(t: datetime, o, h, l, c, v) -> Bar:
    return Bar(t.astimezone(timezone.utc), D(str(o)), D(str(h)), D(str(l)), D(str(c)), int(v))


def settings(tmp_path, **over):
    raw = {"runtime": {"data_dir": str(tmp_path / "rt")},
           "safety": {"kill_switch_file": str(tmp_path / "KILL")},
           "schedule": {"eod_cancel_timeout_seconds": 30}}
    for k, v in over.items():
        sec, key = k.split("__")
        raw.setdefault(sec, {})[key] = v
    return parse_settings(raw)


SYMBOLS = [f"S{chr(65 + i)}{chr(65 + j)}" for i in range(3) for j in range(10)][:25]  # SAA..SCE


class FakeData:
    """Deterministic data: 25 liquid symbols, price ~50, ATR ~2, ADV 2M.
    Opening-volume history 10,000/day; today's opening volume ranks the names.
    Longs (even index) open 50 close 50.5; shorts (odd) open 50 close 49.5."""

    def __init__(self, cal: MarketCalendar, symbols=SYMBOLS, missing_today=(), today_volume=None,
                 minute=None):
        self.cal = cal
        self.symbols = list(symbols)
        self.missing_today = set(missing_today)
        self.today_volume = today_volume or {s: 10_000 + 1_000 * i for i, s in enumerate(self.symbols)}
        self.minute = minute or {}
        self.calls = {"opening_bars": 0, "assets": 0, "rth_daily": 0, "minute": 0}

    def assets(self, day):
        self.calls["assets"] += 1
        rows = [{"symbol": s, "tradable": True, "shortable": True, "easy_to_borrow": True,
                 "exchange": "NASDAQ", "status": "active"} for s in self.symbols]
        rows.append({"symbol": "SPY", "tradable": True, "shortable": True, "easy_to_borrow": True,
                     "exchange": "ARCA", "status": "active"})
        return rows

    def rth_daily_bars(self, symbols, sess):
        self.calls["rth_daily"] += 1
        return {s: {x.day: bar(x.open, 50, 51, 49, 50, 2_000_000) for x in sess} for s in symbols}

    def opening_bars_history(self, symbols, sess):
        return {s: {x.day: bar(x.open, 50, 50.4, 49.6, 50.1, 10_000) for x in sess} for s in symbols}

    def corporate_action_snapshot(self, symbols, day):
        return {'observed_at': et(day, 9, 0).isoformat(), 'symbols': list(symbols),
                'actions': [], 'complete': True}

    def opening_bars(self, symbols, session, purpose="scan"):
        self.calls["opening_bars"] += 1
        out = {}
        for i, s in enumerate(self.symbols):
            if s in self.missing_today or s not in symbols:
                continue
            if i % 2 == 0:
                out[s] = bar(session.open, 50, 51, 49.8, 50.5, self.today_volume[s])
            else:
                out[s] = bar(session.open, 50, 50.2, 49, 49.5, self.today_volume[s])
        return out

    def entry_quotes(self, symbols, now):
        """Fresh, tight IEX quotes that do not cross the triggers: longs trade just below their level, shorts just
        above it (the entry gate fails closed when there is no quote, so the fixture must provide one)."""
        out = {}
        for i, s in enumerate(self.symbols):
            if s in symbols:
                bid, ask = (D("50.40"), D("50.50")) if i % 2 == 0 else (D("49.50"), D("49.60"))
                out[s] = Quote(s, bid, ask, t=now - timedelta(milliseconds=300))
        return out

    def minute_bars_iex(self, symbols, start, end):
        self.calls["minute"] += 1
        return {s: [b for b in self.minute.get(s, []) if start <= b.t < end] for s in symbols}


class Harness:
    """Engine + SimBroker + journal for one session, with a pump for events."""

    def __init__(self, tmp_path, day=date(2026, 9, 24), now=None, early=False, journal=None, sim=None,
                 q=None, entry_cutoff=None, **over):
        self.day = day
        self.session = Session(day, et(day, 9, 30), et(day, 13 if early else 16, 0))
        self.settings = settings(tmp_path, **over)
        self.clock = FakeClock(now or et(day, 9, 35, 3))
        self.q = q or queue.Queue()
        self.sim = sim or SimBroker(self.q.put, clock=self.clock.now, auto_fill_market=False)
        self.j = journal or Journal(tmp_path / "journal.sqlite")
        self.schedule = Schedule.for_session(self.session, 3, 120, 60, self.settings.entry_cutoff_time)
        if entry_cutoff is not None:
            self.schedule = replace(self.schedule, entry_cutoff=entry_cutoff)
        elif self.clock.now() >= self.schedule.entry_cutoff:
            # legacy end-of-day scenarios place their entry late in the day: only the cutoff is moved to the
            # EOD window (an entry after eod_exit is still refused)
            self.schedule = replace(self.schedule, entry_cutoff=self.schedule.eod_exit)
        self.engine = Engine(self.j, self.sim, self.settings, self.clock, "paper")
        self.engine.start_day(self.schedule)
        self.plans_made: dict[str, EntryPlan] = {}
        self.engine.quote_source = self.default_quotes          # tests override it to exercise the gate

    def open_session(self):
        self.j.mark_scanned(self.day, "ok")
        self.j.set_session_closing(self.day, False)

    def pump(self):
        for _ in range(10):                  # like the runner: events, then queued stop changes, repeat
            while not self.q.empty():
                self.engine.on_update(self.q.get())
            self.engine.flush_stops()
            if self.q.empty():
                break

    def drop_events(self):
        while not self.q.empty():
            self.q.get()

    def default_quotes(self, symbols, now):
        """A fresh, tight quote that does not cross the trigger of any plan made with `plan()` (long: ask just
        below the trigger; short: bid just above it). A symbol never planned has no quote (= fail closed)."""
        out = {}
        for s in symbols:
            p = self.plans_made.get(s) or self.j.trade(self.day.isoformat(), s)
            if p is None:
                continue
            step = max(Decimal("0.02"), (p.entry_price * Decimal("0.001")).quantize(Decimal("0.01")))
            if p.direction > 0:
                ask = p.entry_price - step
                bid = ask - step
            else:
                bid = p.entry_price + step
                ask = bid + step
            out[s] = Quote(s, bid, ask, t=now - timedelta(milliseconds=300))
        return out

    def plan(self, symbol="AAA", direction=1, qty=100, entry="50.00", stop="49.80"):
        p = self._plan(symbol, direction, qty, entry, stop)
        self.plans_made[symbol] = p
        return p

    def _plan(self, symbol, direction, qty, entry, stop):
        return EntryPlan(symbol, direction, D(entry), D(stop), D(entry), D(stop), D("2"), qty, {"binding": "test"})

    def enter(self, symbol="AAA", direction=1, qty=100, **kw):
        self.j.save_candidates(self.day, [{"symbol": symbol, "rank": 1, "status": "candidate"}])
        self.engine.place_entries([self.plan(symbol, direction, qty, **kw)],
                                  {symbol: {"tradable": True, "shortable": True, "easy_to_borrow": True}},
                                  {"buying_power": "1000000", "shorting_enabled": True}, self.clock.now())
        self.pump()
        return self.j.trade(self.day.isoformat(), symbol)

    def trade(self, symbol="AAA"):
        return self.j.trade(self.day.isoformat(), symbol)

    def order(self, oid):
        return self.sim.orders[oid]

    def orders_by_role(self, role, symbol="AAA"):
        return [o for o in self.j.orders(self.day.isoformat(), symbol) if o.role == role]


def bot_leftover(journal, symbol: str, qty, day: str = "2026-09-23"):
    """An ORB-IEX position left from an earlier session: an orbiex- order and its fill."""
    from orb_iex.journal import OrderRec
    q = D(str(qty))
    rec = OrderRec(order_id=f"old-{symbol}", client_order_id=f"orbiex-{day.replace('-', '')}-{symbol}-entry",
                   day=day, symbol=symbol, role="entry", side="buy" if q > 0 else "sell", type="stop",
                   qty=abs(q), stop_price=D("10"), status="filled", filled_qty=abs(q))
    journal.save_order(rec)
    journal.add_fill(rec, abs(q), abs(q), D("10"), f"{day}T15:00:00+00:00", D(0), D(0))


def unrelated_order(sim, symbol="MANUAL", qty=7):
    """An order the user placed by hand in the same paper account (no orbiex- prefix)."""
    from orb_iex.orders import OrderRequest
    return sim.submit_order(OrderRequest(symbol, "buy", D(qty), "stop", "manual-order-1", D("999")))
