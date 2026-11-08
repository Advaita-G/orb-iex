"""Restart in every trade state: a new Engine on the same journal and the same
(simulated) Alpaca account must converge to a safe state without duplicates."""

from datetime import date
from decimal import Decimal as D

from helpers import Harness, bot_leftover, et
from orb_iex.engine import Engine
from orb_iex.journal import TradeRec
from orb_iex.orders import OrderRequest

DAY = date(2026, 9, 24)


def restart(h, now=None):
    """Simulate a crash: in-flight events are lost; a new engine recovers."""
    h.drop_events()
    if now:
        h.clock.set(now)
    e = Engine(h.j, h.sim, h.settings, h.clock, "paper")
    e.quote_source = h.default_quotes
    e.start_day(h.schedule)
    e.recover(h.clock.now())
    h.engine = e
    h.pump()
    return e


def entries_sent(h):
    return [r for r in h.sim.submitted if r.client_order_id.endswith("-entry")]


def test_restart_planned_not_submitted_inside_grace_submits_once(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.j.save_trade(TradeRec(day=DAY.isoformat(), symbol="AAA", direction=1, planned_qty=D(100),
                            entry_price=D(50), stop_price=D("49.8"), atr=D(2),
                            entry_client_id="orbiex-20260924-AAA-entry"))
    restart(h, et(DAY, 9, 35, 30))
    assert len(entries_sent(h)) == 1 and h.trade().state == "entry_open"
    restart(h, et(DAY, 9, 35, 40))
    assert len(entries_sent(h)) == 1


def test_restart_planned_after_grace_is_not_submitted(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.j.save_trade(TradeRec(day=DAY.isoformat(), symbol="AAA", direction=1, planned_qty=D(100),
                            entry_price=D(50), stop_price=D("49.8"), atr=D(2),
                            entry_client_id="orbiex-20260924-AAA-entry"))
    restart(h, et(DAY, 12, 0))
    assert entries_sent(h) == [] and h.trade().state == "not_submitted"


def test_crash_after_submit_before_journal_write_adopts_order(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.j.save_trade(TradeRec(day=DAY.isoformat(), symbol="AAA", direction=1, planned_qty=D(100),
                            entry_price=D(50), stop_price=D("49.8"), atr=D(2),
                            entry_client_id="orbiex-20260924-AAA-entry"))
    h.sim.submit_order(OrderRequest("AAA", "buy", D(100), "stop", "orbiex-20260924-AAA-entry", D(50)))
    restart(h, et(DAY, 9, 35, 20))
    assert len(entries_sent(h)) == 1
    assert h.trade().entry_order_id is not None and h.trade().state == "entry_open"


def test_restart_at_noon_with_entry_open_changes_nothing(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    before = list(h.sim.write_log)
    restart(h, et(DAY, 12, 0))
    assert h.sim.write_log[len(before):] == [('cancel', h.trade().entry_order_id)]
    assert h.trade().state == 'entry_canceled'


def test_restart_with_protected_position_changes_nothing(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50))
    h.pump()
    before = list(h.sim.write_log)
    restart(h, et(DAY, 12, 0))
    assert h.sim.write_log == before
    assert h.engine.current_stop(h.trade()).qty == D(100)


def test_fill_while_down_without_stop_recreates_planned_stop(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50))   # the bot crashes before seeing it
    restart(h, et(DAY, 12, 0))
    stop = h.engine.current_stop(h.trade())
    assert stop is not None and stop.qty == D(100) and stop.stop_price == D("49.80")
    assert len(h.orders_by_role("stop")) == 1


def test_more_fills_while_down_resize_stop(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(30), D(50))
    h.pump()
    h.sim.fill(t.entry_order_id, D(70), D(50))
    restart(h, et(DAY, 12, 0))
    assert h.engine.current_stop(h.trade()).qty == D(100)


def test_stop_filled_while_down_cancels_entry_remainder(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(40), D(50))
    h.pump()
    stop = h.engine.current_stop(h.trade())
    h.sim.fill(stop.order_id, D(40), D("49.8"))
    restart(h, et(DAY, 12, 0))
    t = h.trade()
    assert t.closed and h.sim.orders[t.entry_order_id].status == "canceled"


def test_stop_canceled_while_down_flattens(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50))
    h.pump()
    h.sim.cancel_order(h.engine.current_stop(h.trade()).order_id)
    restart(h, et(DAY, 12, 0))
    assert h.trade().closed and len(h.orders_by_role("stopfail")) == 1


def test_restart_during_eod_wait_continues_liquidation(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50))
    h.pump()
    h.clock.set(et(DAY, 15, 55))
    h.sim.cancel_mode = "pending"
    h.engine.begin_eod(h.clock.now())
    h.pump()
    h.sim.complete_cancels()
    restart(h, et(DAY, 15, 56))
    h.engine.tick(h.clock.now())
    assert len(h.orders_by_role("eod")) == 1


def test_unrelated_position_is_never_touched_mid_session(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.sim.positions["ZZZ"] = D(10)                   # not from any ORB-IEX order
    restart(h, et(DAY, 12, 0))
    h.engine.tick(h.clock.now())
    assert h.sim.write_log == [] and h.sim.positions["ZZZ"] == D(10)
    assert h.engine.bot_positions() == {}


def test_orphan_bot_position_is_flattened_mid_session(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    bot_leftover(h.j, "OWN", 10)                     # ORB-IEX fill from yesterday
    h.sim.positions["OWN"] = D(10)
    h.sim.positions["ZZZ"] = D(10)                   # unrelated
    restart(h, et(DAY, 12, 0))
    h.engine.tick(h.clock.now())
    exits = [o for o in h.j.orders(DAY.isoformat(), "OWN") if o.role == "flatten"]
    assert len(exits) == 1 and exits[0].qty == D(10) and exits[0].side == "sell"
    assert h.j.orders(DAY.isoformat(), "ZZZ") == [] and h.sim.positions["ZZZ"] == D(10)


def test_bot_share_capped_when_user_reduced_position(tmp_path):
    h = Harness(tmp_path, now=et(DAY, 8, 0))
    bot_leftover(h.j, "OWN", 10)
    h.sim.positions["OWN"] = D(4)                    # user sold 6 by hand
    restart(h, et(DAY, 8, 0))                        # pre-open: cap is saved
    assert h.engine.bot_positions() == {"OWN": D(4)}
    h.sim.positions.pop("OWN")
    h.sim.positions["OWN"] = D(50)                   # user later buys 50 of their own
    assert h.engine.bot_positions() == {"OWN": D(4)}


def test_unknown_position_after_close_is_left_for_open_sweep(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.sim.positions["ZZZ"] = D(10)
    restart(h, et(DAY, 17, 0))
    h.engine.tick(h.clock.now())
    assert h.sim.write_log == []


def test_position_without_plan_is_flattened(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50))
    h.pump()
    # journal lost its stop price (not enough data to recreate the stop)
    t = h.trade()
    t.stop_price = D(0)
    h.j.save_trade(t)
    h.sim.cancel_order(h.engine.current_stop(t).order_id)
    rec = h.j.order(h.trade().stop_order_id)
    rec.cancel_requested = True
    h.j.save_order(rec)
    restart(h, et(DAY, 12, 0))
    h.engine.tick(h.clock.now())
    assert len(h.orders_by_role("flatten")) == 1
