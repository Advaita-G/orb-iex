"""Clean shutdown safety: Ctrl+C / SIGTERM must not leave a live ORB-IEX entry order at Alpaca. New entries are blocked at
once, every unfilled bot-owned entry of the session is cancelled and CONFIRMED terminal (bounded by
runtime.shutdown_timeout_seconds), a fill that wins the cancel race is protected or flattened before the process exits,
protective stops of positions that were already open are left alone, manual/foreign orders are never touched."""

import json
import os
import signal
from decimal import Decimal as D
from pathlib import Path

import pytest

from helpers import Harness, unrelated_order
from orb_iex import cli
from orb_iex.config import ConfigError, parse_settings
from orb_iex.runner import SHUTDOWN_FILE
from orb_iex.tradelog import TradeLog
from test_fatal import held, runner_for

SYMS = ("MUB", "CG", "CRBG", "AVDE")                    # the four entries still `new` after today's Ctrl+C


def harness(tmp_path, **over):
    h = Harness(tmp_path, **over)
    h.open_session()
    h.engine.tlog = TradeLog(tmp_path / "tl.sqlite", tmp_path / "exports")
    return h


def four_entries(h):
    h.j.save_candidates(h.day, [{"symbol": s, "rank": i + 1, "status": "candidate"} for i, s in enumerate(SYMS)])
    assets = {s: {"tradable": True, "shortable": True, "easy_to_borrow": True} for s in SYMS}
    plans = [h.plan("MUB", 1, 100, entry="105.00", stop="104.80"), h.plan("CG", -1, 80, entry="60.00", stop="60.20"),
             h.plan("CRBG", 1, 120, entry="40.00", stop="39.80"), h.plan("AVDE", -1, 90, entry="90.96", stop="91.06")]
    h.engine.place_entries(plans, assets, {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    h.pump()
    assert {h.trade(s).state for s in SYMS} == {"entry_open"}


def ctrl_c(r):
    """Press Ctrl+C inside the run loop; returns the report the shutdown wrote."""
    def stop(*_a, **_k):
        raise KeyboardInterrupt()
    r.step = stop
    from orb_iex.runner import FatalRunnerError
    with pytest.raises((KeyboardInterrupt, FatalRunnerError)) as caught:
        r.run()
    report = json.loads((Path(r.settings.data_dir) / SHUTDOWN_FILE).read_text())
    assert isinstance(caught.value, KeyboardInterrupt if report['complete'] else FatalRunnerError)
    return report


def live_entries(h):
    return [o for o in h.sim.orders.values() if o.client_order_id.endswith("-entry")
            and o.status not in ("canceled", "filled", "expired", "rejected", "replaced")]


def events(h, kind, sym=None):
    out = h.engine.tlog.events(h.day.isoformat(), sym) if sym else h.engine.tlog.events(h.day.isoformat())
    return [e for e in out if e["kind"] == kind]


def detail(e):
    d = e["detail"]
    return d if isinstance(d, dict) else json.loads(d)


# --- Ctrl+C with four unfilled entries ----------------------------------------------------------------------------------------

def test_ctrl_c_with_four_unfilled_entries_cancels_all_of_them_and_none_stays_live(tmp_path):
    h = harness(tmp_path)
    four_entries(h)
    assert len(live_entries(h)) == 4
    manual = unrelated_order(h.sim, "MANUAL")                       # the user's own order, same account
    h.sim.positions["USER"] = D(25)
    r = runner_for(h)
    rep = ctrl_c(r)
    assert live_entries(h) == []                                    # nothing that can fill while the bot is gone
    assert all(h.sim.orders[h.trade(s).entry_order_id].status == "canceled" for s in SYMS)   # at the broker
    assert all(h.j.order(h.trade(s).entry_order_id).status == "canceled" for s in SYMS)      # and in the journal
    assert rep["complete"] is True and rep["errors"] == []
    assert {s: e["action"] for s, e in rep["entries"].items()} == {s: "entry_cancel_confirmed" for s in SYMS}
    assert r.engine.entries_blocked == "shutdown"
    assert {h.trade(s).state for s in SYMS} == {"entry_shutdown_canceled"}
    assert {c["reason"] for c in h.j.candidates(h.day) if c["symbol"] in SYMS} == {"shutdown_entry_canceled"}
    # the user's own order and position are untouched
    assert h.sim.orders[manual.order_id].status == "new" and ("cancel", manual.order_id) not in h.sim.write_log
    assert h.sim.positions == {"USER": D(25)}


def test_journal_events_for_a_successful_shutdown(tmp_path):
    h = harness(tmp_path)
    four_entries(h)
    ctrl_c(runner_for(h))
    assert len(events(h, "shutdown_requested")) == 1 and detail(events(h, "shutdown_requested")[0])["reason"]
    assert sorted(e["symbol"] for e in events(h, "entry_cancel_requested")) == sorted(SYMS)
    assert sorted(e["symbol"] for e in events(h, "entry_cancel_confirmed")) == sorted(SYMS)
    assert all(e["status"] == "canceled" for e in events(h, "entry_cancel_confirmed"))
    for kind in ("fill_won_cancel_race", "position_protected_on_shutdown", "position_flattened_on_shutdown",
                 "shutdown_incomplete"):
        assert events(h, kind) == []                                # nothing else happened


def test_new_entries_are_blocked_the_moment_the_shutdown_starts(tmp_path):
    h = harness(tmp_path)
    four_entries(h)
    seen = {}
    real = h.sim.cancel_order

    def spy(order_id):
        seen["blocked"] = h.engine.entries_blocked                 # at the first cancel the block is already on
        return real(order_id)
    h.sim.cancel_order = spy
    ctrl_c(runner_for(h))
    assert seen["blocked"] == "shutdown"
    h.j.save_candidates(h.day, [{"symbol": "NEW", "rank": 9, "status": "candidate"}])
    h.engine.place_entries([h.plan("NEW", 1, 10)], {"NEW": {"tradable": True, "shortable": True,
                                                           "easy_to_borrow": True}},
                           {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    assert h.trade("NEW") is None and live_entries(h) == []


def test_a_cancel_that_stays_pending_is_confirmed_by_reconciling_until_terminal(tmp_path):
    h = harness(tmp_path)
    four_entries(h)
    h.sim.cancel_mode = "pending"                                  # Alpaca says pending_cancel first
    real = h.clock.sleep

    def sleep(s):                                                  # ... and finishes a second later
        h.sim.complete_cancels()
        real(s)
    h.clock.sleep = sleep
    rep = ctrl_c(runner_for(h))
    assert rep["complete"] and live_entries(h) == []
    assert all(h.j.order(h.trade(s).entry_order_id).status == "canceled" for s in SYMS)
    assert len(events(h, "entry_cancel_confirmed")) == 4


def test_a_cancel_the_broker_refuses_is_resent_until_it_works(tmp_path):
    h = harness(tmp_path)
    four_entries(h)
    h.sim.cancel_fail_count = 3
    rep = ctrl_c(runner_for(h))
    assert rep["complete"] and live_entries(h) == [] and h.sim.cancel_fail_count == 0


def test_sigterm_does_the_same_as_ctrl_c(tmp_path):
    h = harness(tmp_path)
    four_entries(h)
    r = runner_for(h)
    old = signal.getsignal(signal.SIGTERM)
    try:
        cli._graceful_sigterm()                                     # what the service does at start-up

        def term(*_a, **_k):
            os.kill(os.getpid(), signal.SIGTERM)                    # systemctl stop
        r.step = term
        with pytest.raises(KeyboardInterrupt):
            r.run()
    finally:
        signal.signal(signal.SIGTERM, old)
    assert live_entries(h) == [] and r.engine.entries_blocked == "shutdown"


# --- a fill wins the cancel race -----------------------------------------------------------------------------------------------

def race_fill(h, sym, qty):
    """The broker fills the entry in the instant our cancel arrives (the cancel then does nothing)."""
    oid = h.trade(sym).entry_order_id
    real = h.sim.cancel_order

    def race(order_id):
        if order_id == oid:
            h.sim.fill(order_id, D(qty), D("105.00"))
            return
        return real(order_id)
    h.sim.cancel_order = race


def test_fill_beats_cancel_the_position_is_flattened_before_exit(tmp_path):
    h = harness(tmp_path)
    four_entries(h)
    race_fill(h, "MUB", 100)
    h.sim.auto_fill_market, h.sim.last_price["MUB"] = True, D("105.00")
    rep = ctrl_c(runner_for(h))
    assert rep["entries"]["MUB"]["action"] == "fill_won_cancel_race"
    assert rep["positions"]["MUB"]["action"] == "flattened"
    assert h.sim.positions.get("MUB", D(0)) == 0 and h.engine.bot_positions() == {}
    assert live_entries(h) == []
    assert len(events(h, "fill_won_cancel_race", "MUB")) == 1 and len(events(h, "position_flattened_on_shutdown", "MUB")) == 1
    assert rep["complete"] is True
    others = [s for s in SYMS if s != "MUB"]
    assert sorted(e["symbol"] for e in events(h, "entry_cancel_confirmed")) == sorted(others)


def test_partial_fill_in_the_race_flattens_the_filled_part_and_cancels_the_rest(tmp_path):
    h = harness(tmp_path)
    four_entries(h)
    oid = h.trade("CG").entry_order_id
    real = h.sim.cancel_order

    def race(order_id):
        if order_id == oid:
            h.sim.fill(order_id, D(30), D("60.00"))               # 30 of 80 sold short, then the cancel lands
        return real(order_id)
    h.sim.cancel_order = race
    h.sim.auto_fill_market, h.sim.last_price["CG"] = True, D("60.00")
    rep = ctrl_c(runner_for(h))
    assert h.j.order(oid).status == "canceled" and h.j.order(oid).filled_qty == D(30)
    assert rep["entries"]["CG"]["action"] == "fill_won_cancel_race" and rep["positions"]["CG"]["action"] == "flattened"
    assert h.sim.positions.get("CG", D(0)) == 0 and live_entries(h) == []


def test_fill_race_with_a_working_stop_is_protected_not_flattened(tmp_path):
    """If a valid protective stop for exactly the shares held is already working, the position is protected as it is."""
    h = harness(tmp_path)
    four_entries(h)
    t = h.trade("MUB")
    h.sim.fill(t.entry_order_id, D(100), D("105.00"))                # filled while the bot was running ...
    h.pump()                                                         # ... and the bot placed its stop
    stop = h.engine.current_stop(h.trade("MUB"))
    rep = ctrl_c(runner_for(h))
    assert rep["positions"]["MUB"]["action"] == "protected"
    assert h.sim.orders[stop.order_id].status == "new" and h.sim.positions["MUB"] == D(100)
    assert h.orders_by_role("flatten", "MUB") == [] and h.orders_by_role("stopfail", "MUB") == []
    assert len(events(h, "position_protected_on_shutdown", "MUB")) == 1


# --- already-open positions keep their protection -------------------------------------------------------------------------

def test_an_existing_protected_position_stays_protected(tmp_path):
    h = harness(tmp_path)
    held(h, "AAA", 100)                                              # filled, protective stop live
    stop = h.engine.current_stop(h.trade("AAA"))
    h.j.save_candidates(h.day, [{"symbol": "BBB", "rank": 2, "status": "candidate"}])
    h.engine.place_entries([h.plan("BBB", 1, 50, entry="60.00", stop="59.80")],
                           {"BBB": {"tradable": True, "shortable": True, "easy_to_borrow": True}},
                           {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    rep = ctrl_c(runner_for(h))
    assert rep["entries"]["BBB"]["action"] == "entry_cancel_confirmed"      # the unfilled entry goes
    assert rep["positions"]["AAA"]["action"] == "protected" and rep["complete"]
    assert h.sim.orders[stop.order_id].status == "new"                      # the protective stop was NOT cancelled
    assert ("cancel", stop.order_id) not in h.sim.write_log
    assert h.sim.positions["AAA"] == D(100)
    assert h.orders_by_role("flatten", "AAA") == []


# --- timeout -----------------------------------------------------------------------------------------------------------------------

def test_cancellation_timeout_is_bounded_and_reported_as_incomplete(tmp_path):
    h = harness(tmp_path, runtime__shutdown_timeout_seconds=5)
    four_entries(h)
    stuck = h.trade("MUB").entry_order_id
    h.sim.cancel_ignored.add(stuck)                                  # accepted, never takes effect
    start = h.clock.now()
    r = runner_for(h)
    rep = ctrl_c(r)
    elapsed = (h.clock.now() - start).total_seconds()
    assert 5 <= elapsed <= 9                                         # bounded: the timeout, not forever
    assert rep["complete"] is False and rep["entries"]["MUB"]["action"] == "cancel_unconfirmed"
    assert all(rep["entries"][s]["action"] == "entry_cancel_confirmed" for s in SYMS if s != "MUB")
    inc = events(h, "shutdown_incomplete")
    assert len(inc) == 1 and detail(inc[0])["problems"] == {"MUB": "cancel_unconfirmed"}
    assert h.sim.orders[stuck].status == "new"                       # honestly reported as still live


def test_a_flatten_that_cannot_complete_is_reported_incomplete(tmp_path):
    h = harness(tmp_path, runtime__shutdown_timeout_seconds=5)
    four_entries(h)
    race_fill(h, "MUB", 100)
    h.sim.reject_roles = {"flatten", "stopfail", "eod"}              # the broker refuses every exit
    rep = ctrl_c(runner_for(h))
    assert rep["positions"]["MUB"]["action"] == "flatten_not_confirmed" and rep["complete"] is False
    inc = events(h, "shutdown_incomplete")
    assert len(inc) == 1 and "MUB" in detail(inc[0])["problems"]


def test_broker_unreachable_is_reported_not_hung(tmp_path):
    h = harness(tmp_path)
    four_entries(h)

    def down(*a, **k):
        raise ConnectionError("Alpaca unreachable")
    h.sim.list_orders = down
    rep = ctrl_c(runner_for(h))
    assert rep["complete"] is False and "unreachable" in rep["errors"][0]
    assert len(events(h, "shutdown_incomplete")) == 1


def test_a_second_ctrl_c_during_the_shutdown_ends_it_without_hanging(tmp_path):
    h = harness(tmp_path)
    four_entries(h)
    h.sim.cancel_ignored.add(h.trade("MUB").entry_order_id)

    def again(_s):
        raise KeyboardInterrupt()                                    # the user presses Ctrl+C again
    h.clock.sleep = again
    rep = ctrl_c(runner_for(h))
    assert rep["complete"] is False and "interrupted again" in rep["errors"][0]
    assert len(events(h, "shutdown_incomplete")) == 1


# --- other cases -------------------------------------------------------------------------------------------------------------------

def test_an_orbiex_entry_that_only_alpaca_knows_is_found_and_cancelled(tmp_path):
    from orb_iex.orders import OrderRequest
    h = harness(tmp_path)
    h.sim.submit_order(OrderRequest("CBRS", "sell", D(45), "stop", "orbiex-20260924-CBRS-entry", D("177.86")))
    h.drop_events()                                                  # the journal never saw it
    rep = ctrl_c(runner_for(h))
    assert rep["entries"]["CBRS"]["action"] == "entry_cancel_confirmed" and live_entries(h) == []


def test_only_todays_orbiex_entries_are_cancelled_not_other_days_or_manual_orders(tmp_path):
    from orb_iex.orders import OrderRequest
    h = harness(tmp_path)
    four_entries(h)
    manual = unrelated_order(h.sim, "MANUAL")
    h.sim.submit_order(OrderRequest("OLD", "buy", D(5), "stop", "orbiex-20250102-OLD-entry", D("9")))
    old = next(o.order_id for o in h.sim.orders.values() if o.symbol == "OLD")
    ctrl_c(runner_for(h))
    assert h.sim.orders[manual.order_id].status == "new" and h.sim.orders[old].status == "canceled"


def test_ctrl_c_before_a_session_exists_does_not_crash(tmp_path):
    h = harness(tmp_path)
    r = runner_for(h)
    r.engine.day = None
    r.engine.schedule = None
    rep = ctrl_c(r)
    assert rep["complete"] is False and "no session" in rep["errors"][0]


def test_signal_mode_has_no_orders_to_cancel(tmp_path):
    h = harness(tmp_path)
    r = runner_for(h, mode="signal")
    r.engine, r.broker = None, None

    def stop(*_a, **_k):
        raise KeyboardInterrupt()
    r.step = stop
    with pytest.raises(KeyboardInterrupt):
        r.run()                                                      # a clean stop, nothing to do


def test_shutdown_timeout_is_a_validated_setting():
    assert parse_settings({}).shutdown_timeout_seconds == 20
    assert parse_settings({"runtime": {"shutdown_timeout_seconds": 30}}).shutdown_timeout_seconds == 30
    for bad in (4, 51, "20", True):
        with pytest.raises(ConfigError):
            parse_settings({"runtime": {"shutdown_timeout_seconds": bad}})
    unit = (Path(__file__).resolve().parents[2] / "deploy" / "oracle" / "orb-iex-paper.service").read_text()
    assert "TimeoutStopSec=60" in unit                               # the shutdown (<= 50 s) fits inside it
