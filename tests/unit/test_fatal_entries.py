"""Fatal-error handler, final P0-A gap: when the process dies on an unexpected exception it also cancels every
unfilled ORB-IEX entry order (when Alpaca is reachable), confirms their state in the reconciliation, never touches
unrelated/manual orders, and a fill that wins the cancel race is protected or flattened."""

import json
from decimal import Decimal as D
from pathlib import Path

import pytest

from helpers import Harness, unrelated_order
from orb_iex.broker import OrderRejected
from orb_iex.orders import OrderRequest
from orb_iex.runner import FATAL_FILE, FatalRunnerError
from test_fatal import crash, held, runner_for


def two_entries(h):
    h.open_session()
    h.j.save_candidates(h.day, [{"symbol": s, "rank": i + 1, "status": "candidate"} for i, s in enumerate(("AAA", "BBB"))])
    assets = {s: {"tradable": True, "shortable": True, "easy_to_borrow": True} for s in ("AAA", "BBB")}
    h.engine.place_entries([h.plan("AAA", 1, 100), h.plan("BBB", -1, 50, entry="40.00", stop="40.20")], assets,
                           {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    h.pump()


def entry_status(h, sym):
    return h.j.order(h.trade(sym).entry_order_id).status


def test_fatal_error_cancels_every_unfilled_entry_and_confirms_it(tmp_path):
    h = Harness(tmp_path)
    two_entries(h)
    manual = unrelated_order(h.sim, "MANUAL")                          # the user's own order in the same account
    r = runner_for(h)
    err = crash(r)
    ent = err.report["protect"]["entries"]
    assert ent["AAA"]["action"] == "canceled_confirmed" and ent["BBB"]["action"] == "canceled_confirmed"
    assert entry_status(h, "AAA") == "canceled" and entry_status(h, "BBB") == "canceled"
    assert h.sim.orders[h.trade("AAA").entry_order_id].status == "canceled"        # at the broker too
    assert h.trade("AAA").state == "entry_fatal_canceled"
    assert [c["reason"] for c in h.j.candidates(h.day) if c["symbol"] == "AAA"] == ["fatal_error_entry_canceled"]
    assert h.sim.orders[manual.order_id].status == "new"                           # unrelated order untouched
    assert ("cancel", manual.order_id) not in h.sim.write_log
    assert r.engine.entries_blocked == "fatal_error"


def test_fatal_cancel_is_persisted_with_the_fatal_report(tmp_path):
    h = Harness(tmp_path)
    two_entries(h)
    err = crash(runner_for(h))
    f = json.loads((Path(h.settings.data_dir) / FATAL_FILE).read_text())
    assert f["protect"]["entries"]["AAA"]["action"] == "canceled_confirmed"
    assert "journal" in err.report["persisted"] and "file" in err.report["persisted"]


def test_a_cancel_that_stays_pending_is_confirmed_by_reconciliation_not_assumed(tmp_path):
    h = Harness(tmp_path)
    two_entries(h)
    h.sim.cancel_mode = "pending"                                      # Alpaca answers pending_cancel first
    r = runner_for(h)
    err = crash(r)
    ent = err.report["protect"]["entries"]
    assert ent["AAA"]["action"] == "cancel_unconfirmed" and ent["AAA"]["status"] == "pending_cancel"
    assert h.trade("AAA").state == "entry_open"                        # not claimed canceled
    h.sim.complete_cancels()                                           # the broker finishes; recovery would see it
    r.engine.reconcile(h.clock.now(), side_effects=False)
    assert entry_status(h, "AAA") == "canceled"


def test_a_fill_that_wins_the_cancel_race_is_flattened_not_left_loose(tmp_path):
    h = Harness(tmp_path)
    two_entries(h)
    aaa = h.trade("AAA").entry_order_id
    real = h.sim.cancel_order

    def race(order_id):                                                # the fill wins the race with the cancel
        if order_id == aaa:
            h.sim.fill(order_id, D(100), D("50.00"))
            return
        return real(order_id)
    h.sim.cancel_order = race
    h.sim.auto_fill_market, h.sim.last_price["AAA"] = True, D("50.00")
    err = crash(runner_for(h))
    assert err.report["protect"]["entries"]["AAA"]["action"] == "filled_during_cancel"
    assert err.report["protect"]["positions"]["AAA"]["action"] == "flatten_initiated"     # no stop yet -> flatten
    h.pump()
    assert h.sim.positions.get("AAA", D(0)) == 0


def test_an_open_position_keeps_its_stop_and_only_the_unfilled_entries_are_cancelled(tmp_path):
    h = Harness(tmp_path)
    held(h, "AAA", 100)                                                # filled, protected
    h.j.save_candidates(h.day, [{"symbol": "BBB", "rank": 2, "status": "candidate"}])
    h.engine.place_entries([h.plan("BBB", 1, 50, entry="60.00", stop="59.80")],
                           {"BBB": {"tradable": True, "shortable": True, "easy_to_borrow": True}},
                           {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    stop_id = h.engine.current_stop(h.trade("AAA")).order_id
    err = crash(runner_for(h))
    assert err.report["protect"]["entries"] == {"BBB": {"order_id": h.trade("BBB").entry_order_id,
                                                        "action": "canceled_confirmed", "status": "canceled"}}
    assert err.report["protect"]["positions"]["AAA"]["action"] == "protected"
    assert h.sim.orders[stop_id].status == "new"                       # the protective stop was not touched


def test_the_foreign_order_with_an_orbiex_looking_but_different_day_id_is_not_ours(tmp_path):
    """Keep the historical test name; old bot entries are owned, manual orders are not."""
    h = Harness(tmp_path)
    two_entries(h)
    manual = unrelated_order(h.sim, "MANUAL")
    h.sim.submit_order(OrderRequest("OLD", "buy", D(5), "stop", "orbiex-20250102-OLD-entry", D("9")))
    old_id = next(o.order_id for o in h.sim.orders.values() if o.symbol == "OLD")
    crash(runner_for(h))
    assert h.sim.orders[manual.order_id].status == "new" and h.sim.orders[old_id].status == "canceled"


def test_an_orbiex_entry_that_only_alpaca_knows_is_found_and_cancelled(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.sim.submit_order(OrderRequest("CBRS", "sell", D(45), "stop", "orbiex-20260924-CBRS-entry", D("177.86")))
    h.drop_events()                                                    # the journal never saw it
    err = crash(runner_for(h))
    assert err.report["protect"]["entries"]["CBRS"]["action"] == "canceled_confirmed"
    assert all(o.status == "canceled" for o in h.sim.orders.values())


def test_broker_unreachable_the_handler_does_not_hang_or_raise_and_says_so(tmp_path):
    h = Harness(tmp_path)
    two_entries(h)

    def down(*a, **k):
        raise ConnectionError("Alpaca unreachable")
    h.sim.list_orders = down
    h.sim.cancel_order = down
    err = crash(runner_for(h))
    assert err.report["protect"]["reconciled"] is False and err.report["protect"]["positions"] is None
    assert entry_status(h, "AAA") == "new"                             # nothing was claimed
    assert r_blocked(err)


def r_blocked(err):
    return err.report["entries_blocked"] is True


def test_a_failing_cancel_for_one_entry_does_not_stop_the_others(tmp_path):
    h = Harness(tmp_path)
    two_entries(h)
    real = h.sim.cancel_order
    aaa = h.trade("AAA").entry_order_id

    def picky(order_id):
        if order_id == aaa:
            raise OrderRejected("HTTP 500: server error", 500)
        return real(order_id)
    h.sim.cancel_order = picky
    err = crash(runner_for(h))
    ent = err.report["protect"]["entries"]
    assert ent["BBB"]["action"] == "canceled_confirmed" and ent["AAA"]["action"] == "cancel_unconfirmed"


def test_the_restarted_bot_never_re_enters_a_fatal_cancelled_signal(tmp_path):
    h = Harness(tmp_path)
    two_entries(h)
    crash(runner_for(h))
    h.sim.submitted.clear()
    h.engine.entries_blocked = None
    h.engine.submit_entry(h.trade("AAA"))
    h.engine.place_entries([h.plan("AAA", 1, 100)], {"AAA": {"tradable": True, "shortable": True,
                                                           "easy_to_borrow": True}},
                           {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    assert [r for r in h.sim.submitted if r.client_order_id.endswith("-entry")] == []
