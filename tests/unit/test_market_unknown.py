"""A stale market-open check means "unknown", not "closed".

2026-10-26: during a long reconcile the paper-auto clock check aged past its 10 s freshness
window. The position guard read that as "market closed" for a just-filled EWZ entry, latched the
manual `overnight_position` breaker at 09:40 ET, and blocked every entry for the rest of the day.
Inside the scheduled session an unconfirmed market state must wait for the next fresh check.
"""
from datetime import timedelta
from decimal import Decimal as D

from helpers import Harness
from test_paper_auto import owner, ready  # noqa: F401  (owner is an autouse fixture)


def kinds(h):
    return {b[0] for b in h.j.breakers()}


def filled_without_stop(tmp_path, state):
    """A long entry fills while the market-state hook reports `state` ('open'|'closed'|'unknown')."""
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter("AAA")
    h.engine.regular_market_guard = lambda now: state["v"] == "open"
    h.engine.regular_market_state = lambda now: state["v"]
    h.sim.fill(t.entry_order_id, D(100), D("50.00"))
    h.pump()
    return h


def stops(h):
    return [o for o in h.sim.submitted if o.symbol == "AAA" and o.client_order_id.split("-")[-1].startswith("stop")]


def test_unknown_market_state_in_session_does_not_latch_overnight_breaker(tmp_path):
    state = {"v": "unknown"}
    h = filled_without_stop(tmp_path, state)
    h.engine.reconcile_symbol("AAA")
    assert "overnight_position" not in kinds(h)
    state["v"] = "open"                       # the next fresh clock check confirms the session
    h.engine.reconcile_symbol("AAA")
    h.pump()
    assert "overnight_position" not in kinds(h)
    assert stops(h), "once the market is confirmed open the fill is protected"


def test_broker_confirmed_closed_still_latches_overnight_breaker(tmp_path):
    h = filled_without_stop(tmp_path, {"v": "closed"})
    h.engine.reconcile_symbol("AAA")
    assert "overnight_position" in kinds(h)


def test_outside_the_scheduled_session_unknown_is_treated_as_closed(tmp_path):
    h = filled_without_stop(tmp_path, {"v": "unknown"})
    h.clock.advance((h.schedule.session.close - h.clock.now()).total_seconds() + 60)
    h.engine.reconcile_symbol("AAA")
    assert "overnight_position" in kinds(h)


def test_paper_auto_reports_unknown_when_the_clock_check_is_stale(tmp_path):
    r, h, _ = ready(tmp_path)
    now = h.clock.now()
    r.controls_at, r.controls_valid, r.regular_open = now, True, True
    assert r._regular_market_state(now) == "open"
    assert r._regular_market_state(now + timedelta(seconds=11)) == "unknown"
    r.controls_valid = False
    assert r._regular_market_state(now) == "unknown"
    r.controls_valid, r.regular_open = True, False
    assert r._regular_market_state(now) == "closed"
