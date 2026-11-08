"""Order state machine scenarios against the local simulator (fake broker)."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

from helpers import Harness, et, unrelated_order


def fill_entry(h, qty, price="50.00", symbol="AAA"):
    t = h.trade(symbol)
    h.sim.fill(t.entry_order_id, D(qty), D(price))
    h.pump()
    return h.trade(symbol)


def live_stop(h, symbol="AAA"):
    return h.engine.current_stop(h.trade(symbol))


# --- long / short entries and protective stops ------------------------------------

def test_long_entry_full_fill_places_sell_stop(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    assert t.state == "entry_open"
    entry = h.order(t.entry_order_id)
    assert (entry.side, entry.type, entry.stop_price, entry.qty) == ("buy", "stop", D("50.00"), D(100))
    assert entry.client_order_id == "orbiex-20260924-AAA-entry"
    t = fill_entry(h, 100)
    stop = live_stop(h)
    assert (stop.side, stop.qty, stop.stop_price, stop.role) == ("sell", D(100), D("49.80"), "stop")
    assert stop.client_order_id == "orbiex-20260924-AAA-stop1"
    assert t.protected_qty == D(100) and t.state == "active"


def test_short_entry_places_buy_stop(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter(direction=-1, entry="49.00", stop="49.20")
    assert h.order(t.entry_order_id).side == "sell"
    fill_entry(h, 100, "49.00")
    stop = live_stop(h)
    assert stop.side == "buy" and stop.stop_price == D("49.20")
    assert h.sim.positions["AAA"] == D(-100)


def test_short_skipped_when_not_shortable_or_not_etb(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    plans = [h.plan("NOS", -1), h.plan("HTB", -1)]
    h.j.save_candidates(h.day, [{"symbol": s, "rank": i, "status": "candidate"} for i, s in enumerate(("NOS", "HTB"))])
    h.engine.place_entries(plans, {"NOS": {"tradable": True, "shortable": False},
                                   "HTB": {"tradable": True, "shortable": True, "easy_to_borrow": False}},
                           {"buying_power": "1000000"}, h.clock.now())
    assert h.sim.submitted == []
    reasons = {c["symbol"]: c["reason"] for c in h.j.candidates(h.day)}
    assert reasons == {"NOS": "not_shortable", "HTB": "not_easy_to_borrow"}


# --- partial fills and stop resize ---------------------------------------------

def test_partial_fills_resize_stop_with_replacement_ids(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    fill_entry(h, 30)
    first = live_stop(h)
    assert first.qty == D(30)
    fill_entry(h, 50)       # cumulative 80
    second = live_stop(h)
    assert second.qty == D(80) and second.order_id != first.order_id
    assert second.client_order_id == "orbiex-20260924-AAA-stop2"
    old = h.j.order(first.order_id)
    assert old.status == "canceled" and old.cancel_requested
    t = h.trade()
    assert t.stop_order_id == second.order_id and not t.closed and t.protected_qty == D(80)
    # a 'replaced' event is not a cancel: no flatten happened
    assert h.orders_by_role("stopfail") == []
    fill_entry(h, 20)
    assert live_stop(h).qty == D(100)
    assert h.engine.stats["stop_qty_updates"] == 2


def test_stop_update_failure_exits_only_that_fill(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    fill_entry(h, 30)
    h.sim.reject_replace = True
    fill_entry(h, 20)
    exits = h.orders_by_role("stopupd")
    assert exits == []
    assert live_stop(h).qty == D(50)
    assert h.trade().protected_qty == D(50)
    assert not any(w[0] == 'replace' for w in h.sim.write_log)


# --- stop failures -----------------------------------------------------------------

def test_stop_rejected_on_submit_cancels_entry_and_flattens(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    h.sim.last_price["AAA"] = D("50.10")
    h.sim.auto_fill_market = True
    h.sim.reject_roles = {"stop"}
    t = fill_entry(h, 40)
    assert t.closed and t.state == "stop_failed"
    assert h.order(t.entry_order_id).status == "canceled"
    exits = h.orders_by_role("stopfail")
    assert len(exits) == 1 and exits[0].qty == D(40)
    h.pump()
    assert h.sim.list_positions() == {}


def test_stop_canceled_unexpectedly_flattens(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    fill_entry(h, 100)
    stop = live_stop(h)
    h.sim.cancel_order(stop.order_id)          # canceled by someone else (not requested by the bot)
    h.pump()
    t = h.trade()
    assert t.closed and t.state == "stop_failed"
    assert len(h.orders_by_role("stopfail")) == 1


def test_stop_fill_cancels_entry_remainder(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    fill_entry(h, 40)
    stop = live_stop(h)
    h.sim.fill(stop.order_id, D(40), D("49.80"))
    h.pump()
    t = h.trade()
    assert t.closed and t.state == "stopped_out"
    assert h.order(t.entry_order_id).status == "canceled"
    assert h.sim.list_positions() == {}
    # a later fill of the entry remainder (raced the cancel) is flattened, no new stop
    h.sim.orders[t.entry_order_id].status = "partially_filled"
    fill_entry(h, 10)
    assert len(h.orders_by_role("lateexit")) == 1 and len(h.orders_by_role("stop")) == 1


# --- late fills / closing ---------------------------------------------------------

def test_late_fill_after_close_started_is_flattened(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    h.j.set_session_closing(h.day, True)
    fill_entry(h, 25)
    late = h.orders_by_role("lateexit")
    assert len(late) == 1 and late[0].qty == D(25) and late[0].side == "sell"
    assert h.orders_by_role("stop") == []


def test_eod_cancel_then_liquidate_sequence(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    fill_entry(h, 60)
    h.clock.set(et(h.day, 15, 55))
    h.sim.cancel_mode = "pending"
    h.engine.begin_eod(h.clock.now())
    h.pump()
    # nothing liquidated while cancels are pending
    assert h.orders_by_role("eod") == []
    assert h.j.day(h.day)["eod_status"] == "waiting"
    # the entry remainder fills while its cancel is pending -> late fill exit of exactly that fill
    t = h.trade()
    fill_entry(h, 40)
    assert h.orders_by_role("lateexit") == [] # wait for stop cancellation confirmation
    h.sim.complete_cancels()
    h.pump()
    h.engine.tick(h.clock.now())
    eod = h.orders_by_role("eod")
    # One exit sized from the reconciled 100, including the fill/cancel race.
    late = h.orders_by_role('lateexit')
    assert len(eod + late) == 1 and (eod + late)[0].qty == D(100)
    assert h.j.day(h.day)["eod_status"] == "waiting"
    # stop cancel during EOD is expected, not a stop failure
    assert h.orders_by_role("stopfail") == []
    h.engine.tick(h.clock.now())
    assert len(h.orders_by_role("eod") + h.orders_by_role('lateexit')) == 1


def test_eod_cancel_timeout_still_liquidates_and_logs(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    fill_entry(h, 100)
    h.clock.set(et(h.day, 15, 55))
    h.sim.cancel_mode = "pending"
    h.engine.begin_eod(h.clock.now())
    h.pump()
    h.engine.tick(h.clock.now())
    assert h.orders_by_role("eod") == []
    h.clock.advance(31)
    h.engine.tick(h.clock.now())
    assert h.orders_by_role("eod") == []
    assert any(b[0] == 'stuck_pending_order' for b in h.j.breakers())


def test_close_sweep_skips_symbols_with_pending_exit(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    fill_entry(h, 100)
    h.clock.set(et(h.day, 15, 58))
    h.j.set_session_closing(h.day, True)
    h.engine.market_exit("AAA", "eod", None, "test")      # pending, not filled (no price)
    h.engine.sweep("closesweep", "Close sweep")
    assert h.orders_by_role("closesweep") == []


# --- idempotency ---------------------------------------------------------------------

def test_duplicate_events_do_not_double_count(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50))
    events = []
    while not h.q.empty():
        events.append(h.q.get())
    for u in events + events + events:
        h.engine.on_update(u)
    assert len(h.orders_by_role("stop")) == 1
    assert len(h.j.fills(h.day.isoformat())) == 1


def test_out_of_order_events_are_ignored(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(30), D(50))
    partial = h.q.get()
    h.sim.fill(t.entry_order_id, D(70), D(50))
    full = h.q.get()
    h.engine.on_update(full)          # newest first
    h.engine.on_update(partial)       # stale: cum 30 < 100
    rec = h.j.order(t.entry_order_id)
    assert rec.filled_qty == D(100) and rec.status == "filled"
    assert live_stop(h).qty == D(100)
    assert h.engine.stats["out_of_order_ignored"] >= 1


def test_duplicate_order_prevention(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    h.enter()            # a second scan attempt: symbol already has an open order
    assert len([r for r in h.sim.submitted if r.client_order_id.endswith("-entry")]) == 1
    # the same client_order_id sent twice returns the existing order
    req = h.sim.submitted[0]
    again = h.sim.submit_order(req)
    assert again.order_id == h.trade().entry_order_id


def test_kill_switch_blocks_new_entries_only(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter("OLD")
    (tmp_path / "KILL").touch()
    h.enter("NEW")
    assert h.trade("NEW") is None
    assert [c["reason"] for c in h.j.candidates(h.day) if c["symbol"] == "NEW"] == ["kill_switch"]
    # protective stops and exits still work
    t = h.trade("OLD")
    h.sim.fill(t.entry_order_id, D(100), D(50))
    h.pump()
    assert live_stop(h, "OLD") is not None


def test_exit_rejection_is_retried_after_blockers_clear(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    fill_entry(h, 100)
    h.sim.reject_roles = {"stopfail"}
    stop = live_stop(h)
    h.sim.cancel_order(stop.order_id)
    h.pump()
    t = h.trade()
    assert t.pending_flatten
    h.sim.reject_roles = set()
    h.engine.tick(h.clock.now())
    exits = h.orders_by_role('stopfail')
    assert len(exits) == 1 and exits[0].qty == D(100)


def test_eod_leaves_unrelated_orders_and_positions_alone(tmp_path):
    import datetime as dt
    h = Harness(tmp_path)
    h.open_session()
    manual = unrelated_order(h.sim, "MANUAL")
    h.sim.positions["USER"] = D(25)                  # user's own long
    h.enter()
    h.sim.positions["AAA"] = D(50)                   # user buys 50 AAA by hand after the entry
    fill_entry(h, 100)                               # bot adds 100 -> account holds 150 AAA
    assert h.sim.positions["AAA"] == D(150)
    h.clock.set(et(dt.date(2026, 9, 24), 15, 55))
    h.engine.begin_eod(h.clock.now())
    h.pump()
    h.engine.tick(h.clock.now())
    eod = h.orders_by_role("eod")
    assert len(eod) == 1 and eod[0].qty == D(100)   # only the bot's 100, not 150
    assert h.sim.orders[manual.order_id].status == "new"
    assert ("cancel", manual.order_id) not in h.sim.write_log
    h.sim.last_price["AAA"] = D(50)
    h.sim.fill(eod[0].order_id, D(100), D(50))
    h.pump()
    h.engine.sweep("closesweep", "Close sweep")
    assert h.orders_by_role("closesweep") == []
    assert h.sim.positions == {"USER": D(25), "AAA": D(50)}
    summary = h.engine.post_close(h.clock.now())
    assert summary["overnight_positions"] == {}
    assert summary["unrelated_positions_untouched"] == {"USER": "25", "AAA": "50"}
    touched = {w[1] for w in h.sim.write_log if w[0] == "cancel"}
    assert manual.order_id not in touched
    assert all(r.client_order_id.startswith("orbiex-") for r in h.sim.submitted if r.symbol != "MANUAL")


def test_stop_failure_flattens_only_bot_shares(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    h.sim.positions["AAA"] = D(30)                   # user buys 30 AAA by hand after the entry
    fill_entry(h, 100)
    h.sim.cancel_order(live_stop(h).order_id)        # stop lost
    h.pump()
    exits = h.orders_by_role("stopfail")
    assert len(exits) == 1 and exits[0].qty == D(100)


def test_bot_skips_symbol_the_user_already_holds(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.sim.positions["AAA"] = D(30)
    h.enter()
    assert h.sim.submitted == [] and h.trade() is None


def test_protective_stop_uses_actual_fill_price(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()                                        # plan: entry 50.00, ATR 2 -> 0.10 ATR = 0.20
    fill_entry(h, 100, "50.37")                      # filled higher (gap/slippage)
    stop = live_stop(h)
    assert stop.stop_price == D("50.17")             # 50.37 - 0.20, not 49.80
    assert h.trade().stop_price == D("50.17")


def test_short_stop_uses_fill_price_and_resizes_with_average(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter(direction=-1, entry="49.00", stop="49.20")
    fill_entry(h, 50, "48.90")
    assert live_stop(h).stop_price == D("49.10")
    fill_entry(h, 50, "48.70")                       # average fill 48.80
    stop = live_stop(h)
    assert stop.qty == D(100) and stop.stop_price == D("49.00")
    assert h.sim.orders[stop.order_id].stop_price == D("49.00")


def test_no_market_exit_in_last_seconds_before_close(tmp_path):
    import datetime as dt
    h = Harness(tmp_path, now=et(dt.date(2026, 9, 24), 15, 59, 56))
    h.open_session()
    h.sim.positions["AAA"] = D(10)
    from helpers import bot_leftover
    bot_leftover(h.j, "AAA", 10)
    h.engine.sweep("closesweep", "Close sweep")
    assert h.orders_by_role("closesweep") == [] and h.sim.submitted == []


def test_summary_reports_paper_and_realistic_costs(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    fill_entry(h, 100, "50.00")
    h.sim.fill(live_stop(h).order_id, D(100), D("49.80"))
    h.pump()
    s = h.engine.order_summary()
    assert s["gross_pnl"] == "-20.00"
    assert s["net_pnl_paper_costs"] == "-20.70"      # commission 200 sh x 0.0035
    assert s["net_pnl_realistic"] == "-22.70"        # + slippage 200 sh x 0.01
