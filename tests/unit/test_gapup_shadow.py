import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal as D

from helpers import et, settings
from orb_iex import gapup_shadow as g
from orb_iex.marketdata import Quote
from orb_iex.shadow_analytics import portfolio

DAY = date(2026, 11, 2)
CLOSES = [D("100")] * 19 + [D("101")]            # prev close 101 > SMA 100.05


def row(sym="AAA", rank=1, relvol="5", o="103", hi="104", lo="102.5", c="103.5"):
    return {"symbol": sym, "rank": rank, "details": {"relvol": relvol, "open": o, "high": hi, "low": lo, "close": c}}


def q(sym, bid, ask, now, age=0.4, bs=500, a_s=500):
    return Quote(sym, D(str(bid)), D(str(ask)), now - timedelta(seconds=age), "iex", bs, a_s)


def make(tmp_path, name="gapup_top10"):
    s = settings(tmp_path)
    clock = g.TickClock()
    return g.Portfolio(name, g.PORTFOLIOS[name], tmp_path / f"{name}.sqlite", s, D("100000"), clock), clock


def decide(p, rows, hist=None, ok=True):
    inputs = {"ok": ok, "reason": None if ok else "boom", "candidates": rows,
              "history": hist if hist is not None else {r["symbol"]: [str(x) for x in CLOSES] for r in rows},
              "upstream_scan_at": "2026-11-02T14:36:00+00:00", "read_at": "2026-11-02T14:38:00+00:00"}
    with p.j.transaction():
        p.record_decision(DAY, inputs)


def tick(p, clock, n, t, quotes):
    now = t.astimezone(g.timezone.utc)
    clock.current = now
    with p.j.transaction():
        p.step(n, {s: (v(now) if callable(v) else v) for s, v in quotes.items()}, now)


# ---- pure rules
def test_eligible_levels():
    d = g.decide_symbol(row(), CLOSES, 10, D(1))
    assert d["eligible"] and D(d["stop"]) == D("102.5") and D(d["target"]) == D("107.0")
    assert round(D(d["gap_bps"])) == 198


def test_rejection_reasons():
    cases = [(row(rank=11), CLOSES, "rank>10"), (row(relvol="0.5"), CLOSES, "relvol<1"),
             (row(o="5"), CLOSES, "open<=5"), (row(), CLOSES[:5], "ma20_history_incomplete"),
             (row(), None, "ma20_history_incomplete"), (row(o="101.5"), CLOSES, "gap<100bps"),
             (row(hi="102.5", lo="102.5"), CLOSES, "flat_opening_range")]
    for r, closes, why in cases:
        assert g.decide_symbol(r, closes, 10, D(1))["reason"] == why
    falling = [D("110")] * 19 + [D("101")]
    assert g.decide_symbol(row(), falling, 10, D(1))["reason"] == "prior_close_not_above_ma20"


def test_top5_needs_rvol_4_and_rank_5():
    assert g.decide_symbol(row(relvol="3"), CLOSES, 5, D(4))["reason"] == "relvol<4"
    assert g.decide_symbol(row(rank=6, relvol="9"), CLOSES, 5, D(4))["reason"] == "rank>5"
    assert g.decide_symbol(row(relvol="4"), CLOSES, 5, D(4))["eligible"]


def test_entry_and_exit_rules():
    assert g.entry_check(D("102"), D("102.5"), D("107")) == "stop_breached_at_entry"
    assert g.entry_check(D("108"), D("102.5"), D("107")) == "target_passed_at_entry"
    assert g.entry_check(D("104"), D("102.5"), D("107")) is None
    t = g.dtime
    assert g.exit_role(D("102.4"), D("102.5"), D("107"), t(11, 0)) == "stop"
    assert g.exit_role(D("107"), D("102.5"), D("107"), t(11, 0)) == "tgt"
    assert g.exit_role(D("104"), D("102.5"), D("107"), t(11, 0)) is None
    assert g.exit_role(D("104"), D("102.5"), D("107"), t(15, 55)) == "eod"


# ---- simulated sessions on recorded quotes
def run_day(p, clock, script):
    """script: list of (time, {sym: quote-or-callable}) executed as consecutive ticks."""
    for n, (t, quotes) in enumerate(script, 1):
        tick(p, clock, n, t, quotes)


def trips(p):
    return portfolio(p.j.path)["closed_trips"]


def test_target_path_gives_profit_and_independent_ledger(tmp_path):
    p, clock = make(tmp_path)
    decide(p, [row()])
    run_day(p, clock, [
        (et(DAY, 10, 0, 0), {"AAA": lambda n: q("AAA", 103.9, 104.0, n)}),   # entry submitted at ask 104
        (et(DAY, 10, 0, 3), {"AAA": lambda n: q("AAA", 103.9, 104.0, n)}),   # filled at the next valid quote
        (et(DAY, 11, 0, 0), {"AAA": lambda n: q("AAA", 106.9, 107.0, n)}),   # bid below the 107.0 target: no exit
        (et(DAY, 11, 5, 0), {"AAA": lambda n: q("AAA", 107.6, 107.7, n)}),   # bid >= target -> tgt sell
        (et(DAY, 11, 5, 3), {"AAA": lambda n: q("AAA", 107.6, 107.7, n)}),   # exit fill at bid
    ])
    fills = p.j.fills(str(DAY))
    assert [(f["role"], f["side"], f["price"]) for f in fills] == [("entry", "buy", "104.0"), ("tgt", "sell", "107.6")]
    done = trips(p)
    assert len(done) == 1 and done[0]["gross_pnl"] > 0 and done[0]["modeled_cost"] > 0
    assert not p.broker.list_positions()


def test_stop_path_loses_at_bid(tmp_path):
    p, clock = make(tmp_path)
    decide(p, [row()])
    run_day(p, clock, [
        (et(DAY, 10, 0, 0), {"AAA": lambda n: q("AAA", 103.9, 104.0, n)}),
        (et(DAY, 10, 0, 3), {"AAA": lambda n: q("AAA", 103.9, 104.0, n)}),
        (et(DAY, 10, 30, 0), {"AAA": lambda n: q("AAA", 102.3, 102.4, n)}),
        (et(DAY, 10, 30, 3), {"AAA": lambda n: q("AAA", 102.2, 102.3, n)}),
    ])
    done = trips(p)
    assert done[0]["gross_pnl"] < 0 and [f["role"] for f in p.j.fills(str(DAY))] == ["entry", "stop"]
    assert D(p.j.fills(str(DAY))[1]["price"]) == D("102.2")      # gaps through the stop: sold at the observed bid


def test_eod_exit_at_1555(tmp_path):
    p, clock = make(tmp_path)
    decide(p, [row()])
    run_day(p, clock, [
        (et(DAY, 10, 0, 0), {"AAA": lambda n: q("AAA", 103.9, 104.0, n)}),
        (et(DAY, 10, 0, 3), {"AAA": lambda n: q("AAA", 103.9, 104.0, n)}),
        (et(DAY, 15, 55, 0), {"AAA": lambda n: q("AAA", 105.0, 105.1, n)}),
        (et(DAY, 15, 55, 3), {"AAA": lambda n: q("AAA", 105.0, 105.1, n)}),
    ])
    assert [f["role"] for f in p.j.fills(str(DAY))] == ["entry", "eod"]


def test_no_fresh_quote_is_recorded_not_filled(tmp_path):
    p, clock = make(tmp_path)
    decide(p, [row()])
    run_day(p, clock, [
        (et(DAY, 10, 0, 0), {"AAA": lambda n: q("AAA", 103.9, 104.0, n, age=30)}),    # stale
        (et(DAY, 10, 1, 0), {"AAA": None}),                                           # missing
        (et(DAY, 10, 2, 5), {"AAA": lambda n: q("AAA", 103.9, 104.0, n)}),            # after the window
    ])
    st = p.state(DAY)
    assert st["entries"]["AAA"] == {"status": "rejected", "reason": "no_valid_quote_in_window"}
    assert p.j.fills(str(DAY)) == [] and st["quality"]["stale_quote"] >= 1 and st["quality"]["missing_quote"] >= 1


def test_wide_spread_and_crossed_entry_rejected(tmp_path):
    p, clock = make(tmp_path)
    decide(p, [row("WID"), row("LOW", rank=2)])
    run_day(p, clock, [
        (et(DAY, 10, 0, 0), {"WID": lambda n: q("WID", 103.0, 104.0, n),                 # ~97 bps spread > 50
                             "LOW": lambda n: q("LOW", 102.0, 102.1, n)}),               # ask below the stop
        (et(DAY, 10, 2, 5), {"WID": None, "LOW": None}),
    ])
    st = p.state(DAY)
    assert st["entries"]["LOW"]["reason"] == "stop_breached_at_entry"
    assert st["entries"]["WID"]["reason"] == "no_valid_quote_in_window"
    assert p.j.fills(str(DAY)) == []


def test_partial_displayed_size_and_cancel_remainder(tmp_path):
    p, clock = make(tmp_path)
    decide(p, [row()])
    run_day(p, clock, [
        (et(DAY, 10, 0, 0), {"AAA": lambda n: q("AAA", 103.9, 104.0, n, a_s=3)}),
        (et(DAY, 10, 0, 3), {"AAA": lambda n: q("AAA", 103.9, 104.0, n, a_s=3)}),        # only 3 shares displayed
        (et(DAY, 10, 2, 5), {"AAA": lambda n: q("AAA", 103.9, 104.0, n, a_s=3)}),
    ])
    assert [D(f["qty"]) for f in p.j.fills(str(DAY))] == [3, 3]       # 3 displayed shares per quote, nothing more
    assert p.state(DAY)["entries"]["AAA"]["closed_window"] is True
    assert p.broker.orders[p.state(DAY)["entries"]["AAA"]["order_id"]].status == "canceled"


def test_failed_inputs_means_no_trades(tmp_path):
    p, clock = make(tmp_path)
    decide(p, [], ok=False)
    run_day(p, clock, [(et(DAY, 10, 0, 0), {}), (et(DAY, 10, 0, 3), {})])
    assert p.state(DAY)["eligible"] == [] and p.j.fills(str(DAY)) == []
    assert p.j.day(DAY)["scan_status"] == "failed"


def test_two_portfolios_are_independent(tmp_path):
    a, ca = make(tmp_path, "gapup_top10")
    b, cb = make(tmp_path, "gapup_top5")
    rows = [row(relvol="2")]                                  # eligible for Top-10 only
    decide(a, rows)
    decide(b, rows)
    assert a.state(DAY)["eligible"] == ["AAA"] and b.state(DAY)["eligible"] == []
    assert a.j.path != b.j.path


def test_checkpoint_survives_restart(tmp_path):
    p, clock = make(tmp_path)
    decide(p, [row()])
    run_day(p, clock, [(et(DAY, 10, 0, 0), {"AAA": lambda n: q("AAA", 103.9, 104.0, n)}),
                       (et(DAY, 10, 0, 3), {"AAA": lambda n: q("AAA", 103.9, 104.0, n)})])
    held = p.broker.list_positions()
    assert held
    p2, _ = make(tmp_path)                                    # new process, same files
    assert p2.broker.list_positions() == held and p2.state(DAY)["entries"]["AAA"]["status"] == "submitted"


def test_no_broker_write_path_exists():
    import inspect
    src = inspect.getsource(g)
    for word in ("AlpacaPaperBroker", "_write(", "arm(", "trading_post", "api.alpaca.markets"):
        assert word not in src


def test_early_close_exit_time_and_phase_waits():
    assert g.exit_time(None) == g.dtime(15, 55)
    assert g.exit_time(et(DAY, 13, 0)) == g.dtime(12, 55)
    assert g.exit_time(et(DAY, 16, 0)) == g.dtime(15, 55)
    from orb_iex.calendar import Session
    sess = Session(DAY, et(DAY, 9, 30), et(DAY, 16, 0))
    assert g.phase_wait(et(DAY, 8, 0), sess, False) == 60.0           # long idle before the session
    assert g.phase_wait(et(DAY, 9, 45), sess, False) == 5.0           # waiting for the recorded ranking
    assert g.phase_wait(et(DAY, 10, 1), sess, False) == 2.0           # entry window
    assert g.phase_wait(et(DAY, 12, 0), sess, False) == 20.0          # nothing to watch
    assert g.phase_wait(et(DAY, 12, 0), sess, True) == 3.0            # positions to monitor
    assert g.phase_wait(et(DAY, 16, 30), sess, True) == 60.0          # session over: no overnight spin
    assert g.phase_wait(et(DAY, 12, 0), None, True) == 60.0           # non-session day


def test_early_close_forces_exit_before_close(tmp_path):
    p, clock = make(tmp_path)
    p.close = et(DAY, 13, 0)
    decide(p, [row()])
    run_day(p, clock, [
        (et(DAY, 10, 0, 0), {"AAA": lambda n: q("AAA", 103.9, 104.0, n)}),
        (et(DAY, 10, 0, 3), {"AAA": lambda n: q("AAA", 103.9, 104.0, n)}),
        (et(DAY, 12, 55, 0), {"AAA": lambda n: q("AAA", 105.0, 105.1, n)}),
        (et(DAY, 12, 55, 3), {"AAA": lambda n: q("AAA", 105.0, 105.1, n)}),
    ])
    assert [f["role"] for f in p.j.fills(str(DAY))] == ["entry", "eod"]


def test_pre_entry_ticks_are_not_counted_as_missing_quotes(tmp_path):
    p, clock = make(tmp_path)
    decide(p, [row()])
    tick(p, clock, 1, et(DAY, 9, 45), {})                               # before 10:00: no quote request at all
    assert p.state(DAY)["quality"] == {}
