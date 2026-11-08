"""Whole-day runs on a fake clock: scheduling, holidays, early close, DST,
restarts, missing IEX bars, signal vs shadow modes."""

import queue
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from helpers import SYMBOLS, FakeData, bar, bot_leftover, calendar, et, settings, unrelated_order
from orb_iex.clock import FakeClock
from orb_iex.journal import Journal
from orb_iex.runner import Runner
from orb_iex.sim import SimBroker

DAY = date(2026, 9, 24)


@pytest.mark.parametrize("failed_last_page", [False, True])
def test_history_download_keeps_heartbeat_current_between_pages(tmp_path, monkeypatch, failed_last_page):
    from orb_iex import ops
    from orb_iex.http import ApiResponse
    from orb_iex.marketdata import BarCache, MarketData

    clock = FakeClock(et(DAY, 8, 0))
    beats = []
    monkeypatch.setattr(ops, "write_heartbeat", lambda path, body: beats.append(body))

    class Client:
        pages = 0

        def data_get(self, *args, **kwargs):
            self.pages += 1
            clock.advance(45)
            status = 503 if failed_last_page and self.pages == 4 else 200
            return ApiResponse(status, {"bars": {}, "next_page_token":
                                        str(self.pages) if self.pages < 4 else None}, {}, "history")

    client = Client()
    data = MarketData(client, settings(tmp_path), BarCache(tmp_path / "cache.sqlite"),
                      wall_clock=clock.now)
    r, *_ = make(tmp_path, mode="signal", data=data, clock=clock)

    def download():
        return data.fetch_bars(["AAA"], "30Min", et(DAY, 7, 0), et(DAY, 7, 30),
                               "sip", "history:test")

    if failed_last_page:
        with pytest.raises(ConnectionError, match="HTTP 503"):
            download()
    else:
        assert download() == {}
    assert client.pages == 4
    assert len(beats) == 4
    assert beats[-1]["bot_time"] == clock.now().isoformat()
    assert r.last_data_update is None  # partial download is not a completed preparation


def minute_path(day, symbols, up=True):
    """1-minute bars from 09:35 to close that break every long up / short down."""
    out = {}
    for i, s in enumerate(symbols):
        bars = []
        t = et(day, 9, 35)
        while t < et(day, 16, 0):
            if i % 2 == 0:
                bars.append(bar(t, "50.9", "51.3", "50.85", "51.2", 1000))
            else:
                bars.append(bar(t, "49.1", "49.15", "48.7", "48.8", 1000))
            t += timedelta(minutes=1)
        out[s] = bars
    return out


def make(tmp_path, mode="shadow", start=None, cal=None, data=None, journal=None, sim=None, clock=None, **over):
    cal = cal or calendar()
    clock = clock or FakeClock(start or et(DAY, 9, 0))
    q = queue.Queue()
    st = settings(tmp_path, **over)
    data = data or FakeData(cal, minute=minute_path(DAY, SYMBOLS))
    journal = journal or Journal(tmp_path / f"{mode}.sqlite")
    broker = None
    if mode != "signal":
        broker = sim or SimBroker(q.put, clock=clock.now)
        broker.sink = q.put
    r = Runner(st, mode, clock, client=None, data=data, journal=journal, broker=broker, events=q,
               calendar=cal, feed_prefetch=True,
               account_source=lambda: {"equity": "100000", "buying_power": "400000", "shorting_enabled": True})
    return r, clock, data, journal, broker


def test_normal_day_shadow_full_cycle(tmp_path):
    r, clock, data, j, sim = make(tmp_path)
    r.run(until=et(DAY, 16, 5))
    st = j.day(DAY)
    assert st["scan_status"] == "ok" and st["eod_status"] == "liquidated"
    assert {"open_sweep", "diagnostic", "eod", "close_sweep", "post_close"} <= set(st["done"])
    assert data.calls["opening_bars"] == 2            # scan + 09:37 diagnostic, nothing else
    trades = [t for t in j.trades(DAY.isoformat()) if t.state != "foreign"]
    assert len(trades) == 20                            # top 20 of 25 ready names
    top = [c["symbol"] for c in j.candidates(DAY) if c["status"] not in ("filtered", "below_top20")]
    # today's volume rises with the index, so the ranking is the reverse order
    assert top[:3] == [SYMBOLS[24], SYMBOLS[23], SYMBOLS[22]]
    assert sim.list_positions() == {}                   # flat overnight
    assert j.day(DAY)["summary"]["overnight_positions"] == {}
    # entries broke out, stops were placed, then EOD closed everything
    orders = j.orders(DAY.isoformat())
    assert sum(o.role == "entry" and o.status == "filled" for o in orders) == 20
    assert sum(o.role == "eod" for o in orders) == 20
    assert all(o.status in ("canceled", "filled", "replaced") for o in orders if o.role == "stop")


def test_scan_runs_at_093503_not_before(tmp_path):
    r, clock, data, j, _ = make(tmp_path)
    r.run(until=et(DAY, 9, 35, 2))
    assert not j.day(DAY)["scanned"]
    r.run(until=et(DAY, 9, 35, 5))
    assert j.day(DAY)["scan_status"] == "ok"


def test_signal_mode_places_no_orders(tmp_path):
    r, clock, data, j, _ = make(tmp_path, mode="signal")
    r.run(until=et(DAY, 16, 5))
    assert j.day(DAY)["scan_status"] == "ok"
    assert j.orders() == []
    assert sum(c["status"] == "signal_only" for c in j.candidates(DAY)) == 20


def test_holiday_no_scan(tmp_path):
    hol = date(2026, 9, 7)
    r, clock, data, j, _ = make(tmp_path, start=et(hol, 9, 0))
    r.run(until=et(hol, 16, 30))
    assert data.calls["opening_bars"] == 0
    assert not j.day(hol)["scanned"]


def test_early_close_exits_at_1259(tmp_path):
    early = date(2026, 11, 27)
    cal = calendar(end=date(2026, 12, 31), early=(early,))
    data = FakeData(cal, minute=minute_path(early, SYMBOLS))
    r, clock, data, j, sim = make(tmp_path, start=et(early, 9, 0), cal=cal, data=data)
    r.run(until=et(early, 12, 58, 59))
    assert j.day(early)["eod_status"] is None
    r.run(until=et(early, 12, 59, 30))
    assert j.day(early)["eod_status"] == "liquidated"
    r.run(until=et(early, 13, 5))
    assert sim.list_positions() == {} and j.is_done(early, "post_close")


def test_dst_session_runs_on_local_time(tmp_path):
    d = date(2026, 3, 9)                              # first Monday after DST starts
    cal = calendar(start=date(2026, 1, 2), end=date(2026, 4, 30))
    data = FakeData(cal, minute=minute_path(d, SYMBOLS))
    r, clock, data, j, _ = make(tmp_path, start=et(d, 9, 0), cal=cal, data=data)
    r.run(until=et(d, 9, 35, 5))
    assert j.day(d)["scan_status"] == "ok"


def test_restart_at_noon_does_not_rescan_or_duplicate(tmp_path):
    # paper-mode runner with the simulator standing in for the Alpaca paper account
    r, clock, data, j, sim = make(tmp_path, mode="paper")
    r.run(until=et(DAY, 12, 0))
    writes = len(sim.write_log)
    assert len(sim.list_positions()) == 20
    # crash; new runner, same journal and same (simulated) account
    r2, _, data2, _, _ = make(tmp_path, mode="paper", journal=j, sim=sim, clock=clock, data=data)
    r2.run(until=et(DAY, 12, 5))
    assert data.calls["opening_bars"] == 2            # no second scan
    assert sim.write_log[writes:] == []               # stops re-adopted, nothing resubmitted
    r2.run(until=et(DAY, 16, 5))
    assert sim.list_positions() == {}


def test_start_after_scan_window_marks_missed(tmp_path):
    r, clock, data, j, sim = make(tmp_path, start=et(DAY, 10, 0))
    r.run(until=et(DAY, 10, 1))
    assert j.day(DAY)["scan_status"] == "missed"
    assert data.calls["opening_bars"] == 0 and sim.write_log == []


def test_restart_after_close_flattens_only_bot_leftover_after_hours(tmp_path):
    r, clock, data, j, sim = make(tmp_path, start=et(DAY, 17, 0))
    sim.positions["LEFT"] = D(5)                      # not an ORB-IEX position
    bot_leftover(j, "OWN", 3)
    sim.positions["OWN"] = D(3)                       # ORB-IEX position still open after the close
    sim.last_price["OWN"] = D(10)
    r.run(until=et(DAY, 17, 2))
    assert j.is_done(DAY, "post_close")
    s = j.day(DAY)["summary"]
    assert s["overnight_positions"] == {}             # verified flat before the day is closed out
    assert s["unrelated_positions_untouched"] == {"LEFT": "5"}
    after_hours = [r_ for r_ in sim.submitted if r_.client_order_id.endswith("-postclose1")]
    assert len(after_hours) == 1 and after_hours[0].type == "limit" and after_hours[0].extended_hours
    assert after_hours[0].symbol == "OWN" and after_hours[0].side == "sell"
    assert sim.list_positions() == {"LEFT": D(5)}      # the user's shares were never touched
    assert data.calls["opening_bars"] == 0


def test_open_sweep_closes_only_bot_leftover(tmp_path):
    r, clock, data, j, sim = make(tmp_path, start=et(DAY, 9, 0))
    bot_leftover(j, "OWN", 5)
    sim.positions["OWN"] = D(5)
    sim.last_price["OWN"] = D(10)
    sim.positions["LEFT"] = D(5)
    sim.last_price["LEFT"] = D(10)
    r.run(until=et(DAY, 9, 32))
    sweeps = [o for o in j.orders(DAY.isoformat()) if o.role in ('opensweep', 'flatten')]
    assert [(o.symbol, o.qty) for o in sweeps] == [("OWN", D(5))]
    assert sim.list_positions() == {"LEFT": D(5)}


def test_full_day_never_touches_unrelated_orders_or_positions(tmp_path):
    r, clock, data, j, sim = make(tmp_path)
    manual = unrelated_order(sim, "MANUAL")
    sim.positions["USER"] = D(40)
    sim.positions[SYMBOLS[24]] = D(-15)               # user's own short in a symbol the bot trades long
    r.run(until=et(DAY, 16, 5))
    assert sim.orders[manual.order_id].status == "new"
    assert not any(w[0] in ("cancel", "replace") and w[1] == manual.order_id for w in sim.write_log)
    assert sim.list_positions() == {"USER": D(40), SYMBOLS[24]: D(-15)}
    assert all(o.client_order_id.startswith("orbiex-") for o in j.orders())
    assert j.day(DAY)["summary"]["overnight_positions"] == {}


def test_missing_iex_bars_are_not_candidates(tmp_path):
    cal = calendar()
    data = FakeData(cal, missing_today={SYMBOLS[24], SYMBOLS[23]}, minute=minute_path(DAY, SYMBOLS))
    r, clock, data, j, _ = make(tmp_path, data=data, mode="signal")
    r.run(until=et(DAY, 9, 36))
    rows = {c["symbol"]: c for c in j.candidates(DAY)}
    assert rows[SYMBOLS[24]]["status"] == "filtered" and "opening_bar" in rows[SYMBOLS[24]]["reason"]
    top = [c["symbol"] for c in j.candidates(DAY) if c["status"] == "signal_only"]
    assert len(top) == 20 and SYMBOLS[24] not in top


def test_skipped_candidates_are_not_backfilled(tmp_path):
    r, clock, data, j, sim = make(tmp_path)
    orig = data.assets

    def assets(day):
        rows = orig(day)
        for a in rows:
            if a["symbol"] in (SYMBOLS[23], SYMBOLS[21]):   # two top-ranked shorts become hard to borrow
                a["easy_to_borrow"] = False
        return rows
    data.assets = assets
    r.run(until=et(DAY, 9, 36))
    entries = [o for o in j.orders(DAY.isoformat()) if o.role == "entry"]
    assert len(entries) == 18                         # 20 - 2 skipped; ranks 21+ never used
    assert {SYMBOLS[4], SYMBOLS[3]}.isdisjoint({o.symbol for o in entries})


def test_daily_universe_persisted(tmp_path):
    r, clock, data, j, _ = make(tmp_path, mode="signal")
    r.run(until=et(DAY, 9, 1))
    u = j.universe(DAY.isoformat())
    assert u is not None and len(u) == 26 and "SPY" in u     # all eligible listings, ETFs included
    # next session builds its own list
    r.run(until=et(date(2026, 9, 25), 9, 1))
    assert j.universe("2026-09-25") is not None


def test_low_opening_bar_coverage_is_retried(tmp_path):
    cal = calendar()
    data = FakeData(cal, minute=minute_path(DAY, SYMBOLS))
    real = data.opening_bars
    data.opening_bars = lambda syms, s, purpose="scan": {} if data.calls["opening_bars"] == 0 and \
        not data.calls.__setitem__("opening_bars", 1) else real(syms, s, purpose)
    r, clock, data, j, _ = make(tmp_path, data=data, mode="signal")
    r.run(until=et(DAY, 9, 36))
    assert j.day(DAY)["scan_status"] == "ok"
    assert sum(c["status"] == "signal_only" for c in j.candidates(DAY)) == 20
