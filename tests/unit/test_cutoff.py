"""ORB entry cutoff and the end-of-day entry invariant (CBRS, 2026-09-30: a 09:35 entry stop stayed
live all day and filled at 15:59:40, twenty seconds before the close)."""

import datetime as dt
import logging
from dataclasses import replace
from decimal import Decimal as D

import pytest

from helpers import Harness, bot_leftover, et, sessions, settings
from orb_iex.calendar import Schedule
from orb_iex.config import ConfigError, parse_settings
from orb_iex.marketdata import Bar
from orb_iex.orders import OrderRequest

DAY = dt.date(2026, 9, 24)


def to(h, hh, mm, ss=0):
    h.clock.sleep((et(DAY, hh, mm, ss) - h.clock.now()).total_seconds())


def tick(h, seconds=0, step=2):
    for _ in range(max(1, int(seconds / step))):
        h.engine.tick(h.clock.now())
        h.pump()
        h.clock.sleep(step)


def cbrs(h, qty=45):
    """The real CBRS order: SHORT 45, entry stop 177.86, submitted at 09:35:10 and left working."""
    h.j.save_candidates(h.day, [{"symbol": "CBRS", "rank": 1, "status": "candidate"}])
    h.engine.place_entries([h.plan("CBRS", -1, qty, entry="177.86", stop="178.95")],
                           {"CBRS": {"tradable": True, "shortable": True, "easy_to_borrow": True}},
                           {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    h.pump()
    return h.trade("CBRS")


def entry_orders(h, sym="CBRS"):
    return [r for r in h.sim.submitted if r.symbol == sym and r.client_order_id.endswith("-entry")]


def live_stops(h, sym="CBRS"):
    return [o for o in h.j.orders(h.day.isoformat(), sym) if o.role == "stop" and o.status not in
            ("replaced", "canceled", "filled", "expired", "rejected")]


def candidate(h, sym="CBRS"):
    return [c for c in h.j.candidates(h.day) if c["symbol"] == sym][0]


# --- 1. the cutoff is configurable, not hardcoded ---------------------------------------------------------

def test_cutoff_is_a_setting_with_an_operational_default_of_1030():
    # 10:30 ET is an operational default (no entry order lives all day), not a researched strategy parameter
    assert settings(__import__("pathlib").Path("/tmp")).entry_cutoff_time == "10:30"
    assert parse_settings({}).entry_cutoff_time == "10:30"
    s = parse_settings({"schedule": {"entry_cutoff_time": "11:45"}})
    assert s.entry_cutoff_time == "11:45" and "entryCutoff=11:45ET" in s.config_line()


@pytest.mark.parametrize("bad", ["3:30", "15:30:00", "25:00", "15:61", "09:30", "15:45", "noon", 1530])
def test_bad_cutoff_values_are_refused(bad):
    with pytest.raises(ConfigError):
        parse_settings({"schedule": {"entry_cutoff_time": bad}})


def test_schedule_cutoff_follows_the_setting_and_caps_on_early_close_days():
    s = sessions(dt.date(2026, 9, 24), dt.date(2026, 9, 24))[0]
    assert Schedule.for_session(s, 3, 120, 60, "11:45").entry_cutoff == et(DAY, 11, 45)
    assert Schedule.for_session(s, 3, 120, 60).entry_cutoff == et(DAY, 10, 30)
    assert Schedule.for_session(s, 3, 120, 60, "15:30").entry_cutoff == et(DAY, 15, 30)    # still configurable
    early = sessions(dt.date(2026, 11, 27), dt.date(2026, 11, 27), early=[dt.date(2026, 11, 27)])[0]
    sch = Schedule.for_session(early, 3, 120, 60, "15:30")                  # close 13:00: capped at close - 30 min
    assert sch.entry_cutoff == early.close - dt.timedelta(minutes=30)
    assert Schedule.for_session(early, 3, 120, 60, "09:40").entry_cutoff > sch.scan   # never before the scan


# --- 2. cancel at the cutoff, confirm, never re-enter ---------------------------------------------------------

def test_entry_working_before_the_cutoff_is_kept(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = cbrs(h)
    to(h, 10, 29, 50)
    tick(h, 4)
    assert h.order(t.entry_order_id).status == "new" and h.trade("CBRS").state == "entry_open"


def test_cbrs_unfilled_entry_is_cancelled_at_the_cutoff_and_cannot_fill_at_15_59_40(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = cbrs(h)
    to(h, 10, 30, 0)
    tick(h, 4)
    assert h.order(t.entry_order_id).status == "canceled"                       # the broker reports terminal
    assert h.j.order(t.entry_order_id).status == "canceled"                     # and so does the journal
    assert h.trade("CBRS").state == "entry_cutoff_canceled"
    assert candidate(h)["status"] == "skipped" and candidate(h)["reason"] == "entry_cutoff"
    assert h.engine.stats["entry_cutoff_canceled"] == 1
    # 15:59:40: the trigger prints -- there is no live order left to fill
    to(h, 15, 59, 40)
    h.sim.on_bar("CBRS", Bar(et(DAY, 15, 59).astimezone(dt.timezone.utc), D("177.9"), D("177.95"), D("177.80"),
                             D("177.83"), 5000))
    h.pump()
    assert h.sim.positions == {} and live_stops(h) == []
    h.engine.begin_eod(h.clock.now())
    h.pump()
    assert h.engine.post_close(h.clock.now())["overnight_positions"] == {}


def test_cancelled_signal_is_never_re_entered_the_same_day(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = cbrs(h)
    to(h, 10, 30, 0)
    tick(h, 4)
    assert len(entry_orders(h)) == 1
    h.engine.submit_entry(h.trade("CBRS"))                                      # a direct attempt
    h.engine.reconcile(h.clock.now())
    h.engine.recover(h.clock.now())                                             # a restart
    tick(h, 6)
    # a brand-new plan for the same symbol is refused too (window closed)
    h.engine.place_entries([h.plan("CBRS", -1, 45, entry="177.86", stop="178.95")],
                           {"CBRS": {"tradable": True, "shortable": True, "easy_to_borrow": True}},
                           {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    assert len(entry_orders(h)) == 1 and h.trade("CBRS").state == "entry_cutoff_canceled"
    assert [r for r in h.sim.submitted if r.type == "market"] == []


def test_a_cancel_that_fails_is_resent_until_the_broker_confirms_terminal(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = cbrs(h)
    h.sim.cancel_fail_count = 3                                                  # Alpaca refuses the first three
    to(h, 10, 30, 0)
    tick(h, 4)
    assert h.order(t.entry_order_id).status == "new"
    tick(h, 12)
    assert h.order(t.entry_order_id).status == "canceled" and h.sim.cancel_fail_count == 0
    assert h.trade("CBRS").state == "entry_cutoff_canceled"


def test_a_cancel_that_is_ignored_is_never_assumed_done(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = cbrs(h)
    h.sim.cancel_ignored.add(t.entry_order_id)                                   # accepted, but the order stays live
    to(h, 10, 30, 0)
    tick(h, 24)
    assert h.j.order(t.entry_order_id).status == "new" and h.trade("CBRS").state == "entry_cutoff_pending"
    cancels = [w for w in h.sim.write_log if w[0] == "cancel"]
    assert len(cancels) >= 4                                                      # retries only after confirmed live snapshots
    assert t.entry_order_id in h.engine._cutoff_unconfirmed_logged                 # escalated as an ERROR after 15 s


def test_terminal_state_is_confirmed_by_rest_when_the_stream_is_silent(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = cbrs(h)
    to(h, 10, 30, 0)
    h.engine.tick(h.clock.now())                                                 # cancel sent, Alpaca asked directly
    assert not h.q.empty()                                                       # (the stream event is still unread)
    assert h.j.order(t.entry_order_id).status == "canceled"                      # confirmed from Alpaca's own answer
    assert h.trade("CBRS").state == "entry_cutoff_canceled"
    h.drop_events()                                                              # even if the stream never delivers it
    tick(h, 4)
    assert h.j.order(t.entry_order_id).status == "canceled"


# --- 3. the race: a fill while the cancel is pending -------------------------------------------------------------

def test_fill_during_the_cancel_race_is_protected_at_once(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = cbrs(h, 100)
    h.sim.cancel_mode = "pending"                                                # cancel takes a while
    to(h, 10, 30, 0)
    h.engine.tick(h.clock.now())
    h.pump()
    assert h.order(t.entry_order_id).status == "pending_cancel"
    h.sim.orders[t.entry_order_id].status = "new"                                # the broker fills it before cancelling
    h.sim.fill(t.entry_order_id, D(100), D("177.86"))
    h.pump()
    assert h.sim.positions == {"CBRS": D(-100)}
    assert len(live_stops(h)) == 1 and live_stops(h)[0].qty == D(100) and live_stops(h)[0].side == "buy"
    assert h.trade("CBRS").state == "active" and h.engine.stats["cutoff_race_fills"] == 1
    assert candidate(h)["status"] == "submitted"
    assert h.orders_by_role("lateexit", "CBRS") == []                            # protected, not abandoned
    # it is held with its stop and flattened with the rest at the end of the day
    h.sim.auto_fill_market, h.sim.last_price["CBRS"] = True, D("177.5")
    to(h, 15, 59, 0)
    h.engine.begin_eod(h.clock.now())
    tick(h, 40)
    assert not h.orders_by_role('eod', 'CBRS')
    h.sim.cancel_mode = 'immediate'; h.sim.complete_cancels(); h.pump()
    tick(h, 2)
    assert h.sim.positions == {}


def test_partial_fill_in_the_race_protects_the_filled_part_and_cancels_the_rest(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = cbrs(h, 100)
    h.sim.cancel_mode = "pending"
    to(h, 10, 30, 0)
    h.engine.tick(h.clock.now())
    h.sim.orders[t.entry_order_id].status = "new"
    h.sim.fill(t.entry_order_id, D(40), D("177.86"))
    h.sim.orders[t.entry_order_id].status = "pending_cancel"
    h.pump()
    h.sim.complete_cancels()
    h.pump()
    tick(h, 4)
    assert h.j.order(t.entry_order_id).status == "canceled" and h.j.order(t.entry_order_id).filled_qty == D(40)
    assert len(live_stops(h)) == 1 and live_stops(h)[0].qty == D(40)
    assert h.sim.positions == {"CBRS": D(-40)}


# --- 4. end-of-day invariant ----------------------------------------------------------------------------------------

def test_no_new_entry_between_the_cutoff_and_the_close(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    to(h, 10, 40, 0)
    t = cbrs(h)
    assert t.state == "not_submitted" and h.sim.submitted == []
    assert candidate(h)["reason"] == "entry_cutoff"


def test_no_new_entry_once_the_eod_window_begins(tmp_path):
    h = Harness(tmp_path, entry_cutoff=et(DAY, 15, 59, 30))                    # even a cutoff set that late
    h.open_session()
    to(h, 15, 59, 5)
    t = cbrs(h)
    assert t.state == "not_submitted" and candidate(h)["reason"] == "eod_window" and h.sim.submitted == []
    # the last line of defence: nothing can send an entry order once EOD began
    req = OrderRequest("CBRS", "sell", D(45), "stop", "orbiex-20260924-CBRS-entry", stop_price=D("177.86"))
    assert h.engine._submit(req, "entry", "test") is None and h.sim.submitted == []


def test_eod_cancels_entries_before_it_flattens_anything(tmp_path):
    h = Harness(tmp_path, entry_cutoff=et(DAY, 15, 58, 30))                    # the cutoff is later than the test needs
    h.open_session()
    h.j.save_candidates(h.day, [{"symbol": s, "rank": i + 1, "status": "candidate"}
                                for i, s in enumerate(("AAA", "BBB"))])
    assets = {s: {"tradable": True, "shortable": True, "easy_to_borrow": True} for s in ("AAA", "BBB")}
    h.engine.place_entries([h.plan("AAA", 1, 100), h.plan("BBB", 1, 100)], assets,
                           {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    h.pump()
    h.sim.fill(h.trade("AAA").entry_order_id, D(100), D("50.00"))                # AAA is filled, BBB still working
    h.pump()
    h.sim.cancel_mode, h.sim.auto_fill_market, h.sim.last_price["AAA"] = "pending", True, D("50")
    to(h, 15, 59, 0)
    h.engine.begin_eod(h.clock.now())
    h.pump()
    log = h.sim.write_log
    assert log.index(("cancel", h.trade("BBB").entry_order_id)) < min(
        i for i, w in enumerate(log) if w[0] == "submit" and w[1].endswith("-eod1")) if any(
        w[0] == "submit" and w[1].endswith("-eod1") for w in log) else True
    assert not any(w[0] == "submit" and w[1].endswith("-eod1") for w in log), "no exit while cancels are pending"
    h.sim.complete_cancels()
    tick(h, 6)
    assert h.sim.positions == {}                                                   # then it flattens
    assert not [o for o in h.j.orders(h.day.isoformat()) if o.role == "entry" and o.status not in
                ("filled", "canceled", "expired", "rejected")]


# --- 5. restart -------------------------------------------------------------------------------------------------------

def restart(h, now):
    from orb_iex.engine import Engine
    h.drop_events()
    h.clock.set(now)
    e = Engine(h.j, h.sim, h.settings, h.clock, "paper")
    e.quote_source = h.engine.quote_source
    e.start_day(h.schedule)
    e.recover(h.clock.now())
    h.engine = e
    h.pump()
    return e


def test_restart_before_the_cutoff_keeps_the_entry_and_the_cutoff_still_applies(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = cbrs(h)
    restart(h, et(DAY, 10, 0))
    tick(h, 4)
    assert h.order(t.entry_order_id).status == "new" and len(entry_orders(h)) == 1
    to(h, 10, 30, 0)
    tick(h, 4)
    assert h.order(t.entry_order_id).status == "canceled"


def test_restart_after_the_cutoff_cancels_what_is_still_working_and_never_resubmits(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = cbrs(h)
    planned = h.j.save_candidates(h.day, [{"symbol": "NEWP", "rank": 2, "status": "candidate"}])
    from orb_iex.journal import TradeRec
    h.j.save_trade(TradeRec(day=h.day.isoformat(), symbol="NEWP", direction=1, planned_qty=D(10),
                            entry_price=D("50"), stop_price=D("49.8"), atr=D("2"),
                            entry_client_id="orbiex-20260924-NEWP-entry"))     # planned, never submitted (crash)
    restart(h, et(DAY, 10, 41, 0))
    tick(h, 6)
    assert h.order(t.entry_order_id).status == "canceled"                          # found and cancelled
    assert h.trade("NEWP").state == "not_submitted" and entry_orders(h, "NEWP") == []
    assert len(entry_orders(h)) == 1


def test_restart_after_the_cutoff_cancels_an_entry_the_journal_never_knew(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    # an ORB-IEX entry that exists only at Alpaca (the journal was lost)
    h.sim.submit_order(OrderRequest("CBRS", "sell", D(45), "stop", "orbiex-20260924-CBRS-entry", stop_price=D("177.86")))
    h.drop_events()
    restart(h, et(DAY, 10, 41, 0))
    tick(h, 6)
    assert all(o.status == "canceled" for o in h.sim.orders.values())


# --- 6. CBRS 2026-09-30, for any configured cutoff (the 15:30 default is a placeholder, not an approved value) ----

CBRS_DAY = dt.date(2026, 9, 30)


def to_day(h, hh, mm, ss=0):
    h.clock.sleep((et(CBRS_DAY, hh, mm, ss) - h.clock.now()).total_seconds())


@pytest.mark.parametrize("cutoff", ["15:30", "15:00", "14:00", "11:45", "09:40"])
def test_cbrs_2026_09_30_cannot_fill_at_15_59_40_for_any_configured_cutoff(tmp_path, cutoff):
    """The real incident: a SHORT 45 CBRS entry stop at 177.86 was left working from 09:35:10 and filled at 15:59:40,
    twenty seconds before the close. Whatever cutoff the settings carry, the order must be gone by then."""
    h = Harness(tmp_path, day=CBRS_DAY, schedule__entry_cutoff_time=cutoff)
    hh, mm = int(cutoff[:2]), int(cutoff[3:])
    assert h.schedule.entry_cutoff == et(CBRS_DAY, hh, mm) and h.settings.entry_cutoff_time == cutoff
    h.open_session()
    h.clock.sleep(7)                                                            # 09:35:10
    t = cbrs(h)
    assert h.order(t.entry_order_id).status == "new"
    to_day(h, hh, mm, 0)
    for _ in range(3):
        h.engine.tick(h.clock.now())
        h.pump()
        h.clock.sleep(2)
    assert h.order(t.entry_order_id).status == "canceled" and h.j.order(t.entry_order_id).status == "canceled"
    assert h.trade("CBRS").state == "entry_cutoff_canceled"
    to_day(h, 15, 59, 40)
    h.sim.on_bar("CBRS", Bar(et(CBRS_DAY, 15, 59).astimezone(dt.timezone.utc), D("177.9"), D("177.95"),
                             D("177.80"), D("177.83"), 5000))                    # the trigger prints at 15:59:40
    h.pump()
    assert h.sim.positions == {} and live_stops(h) == [] and h.j.owned_qty() == {}
    assert len(entry_orders(h)) == 1                                              # and nothing re-entered
    assert [r for r in h.sim.submitted if r.type == "market"] == []


def test_cbrs_cutoff_restart_cleanup_still_works_with_a_configured_cutoff(tmp_path):
    h = Harness(tmp_path, day=CBRS_DAY, schedule__entry_cutoff_time="14:00")
    h.open_session()
    t = cbrs(h)
    restart(h, et(CBRS_DAY, 14, 30, 0))                                           # the bot was down across the cutoff
    tick(h, 6)
    assert h.order(t.entry_order_id).status == "canceled" and len(entry_orders(h)) == 1


def test_shipped_settings_use_the_operational_default_and_say_it_is_not_researched():
    from orb_iex.config import load_settings
    root = __import__("pathlib").Path(__file__).resolve().parents[2]
    for name in ("settings.toml", "settings.oracle.toml"):
        text = (root / "config" / name).read_text()
        assert 'entry_cutoff_time = "10:30"' in text and "NOT a researched strategy cutoff" in text
        assert "PLACEHOLDER" not in text
        assert load_settings(root / "config" / name).entry_cutoff_time == "10:30"
