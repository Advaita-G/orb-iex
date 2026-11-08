"""Execution-layer rules: crossed ORB entries are skipped (never a market order, never
retried), protective stops are changed only when replaceable and one at a time, a crossed
protective stop triggers the emergency flatten, and REST/stream state merges monotonically."""

import datetime as dt
from dataclasses import replace
from decimal import Decimal as D

from helpers import Harness, et, unrelated_order
from orb_iex.broker import OrderRejected, OrderUncertain, is_crossed_stop_rejection, is_not_replaceable_rejection
from orb_iex.marketdata import Quote, parse_quote


class Quotes:
    """Stands in for MarketData.entry_quotes: a fixed bid/ask per symbol, stamped `age` seconds before the
    request time (fresh by default, as a live quote is)."""

    def __init__(self, age=0, **q):
        self.age = age
        self.q = {s: Quote(s, None if b is None else D(str(b)), None if a is None else D(str(a)))
                  for s, (b, a) in q.items()}
        self.calls = []

    def __call__(self, symbols, now):
        self.calls.append(list(symbols))
        return {s: replace(self.q[s], t=now - dt.timedelta(seconds=self.age)) for s in symbols if s in self.q}


def live_stops(h, symbol="AAA"):
    return [o for o in h.j.orders(h.day.isoformat(), symbol) if o.role == "stop" and o.status not in
            ("replaced", "canceled", "filled", "expired", "rejected")]


def entry_orders_sent(h, symbol="AAA"):
    return [r for r in h.sim.submitted if r.symbol == symbol and r.client_order_id.endswith("-entry")]


def stop_replaces(h):
    return [w for w in h.sim.write_log if w[0] == "replace"]


# --- 1. crossed entries ----------------------------------------------------------------

def test_long_entry_valid_before_trigger_is_submitted(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.engine.quote_source = Quotes(AAA=(49.80, 49.90))          # ask below the 50.00 trigger
    t = h.enter("AAA", 1, 100, entry="50.00", stop="49.80")
    assert t.state == "entry_open" and len(entry_orders_sent(h)) == 1
    assert h.engine.quote_source.calls == [["AAA"]]              # a fresh quote just before the submit
    assert h.order(t.entry_order_id).stop_price == D("50.00")


def test_short_entry_valid_before_trigger_is_submitted(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.engine.quote_source = Quotes(AAA=(50.10, 50.20))          # bid above the 50.00 short trigger
    t = h.enter("AAA", -1, 100, entry="50.00", stop="50.20")
    assert t.state == "entry_open" and len(entry_orders_sent(h)) == 1
    assert h.order(t.entry_order_id).side == "sell"


def test_long_entry_already_crossed_is_skipped_not_market(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.engine.quote_source = Quotes(AAA=(50.05, 50.10))          # ask already above the trigger
    t = h.enter("AAA", 1, 100, entry="50.00", stop="49.80")
    assert t.state == "entry_crossed" and entry_orders_sent(h) == []
    assert h.sim.submitted == [] and h.sim.orders == {}          # no stop entry, no market order
    cand = [c for c in h.j.candidates(h.day) if c["symbol"] == "AAA"][0]
    assert cand["status"] == "skipped" and cand["reason"] == "crossed_before_submit"
    assert h.engine.stats["entry_crossed_skipped"] == 1 and h.engine.stats["entry_crossed_422"] == 0
    # never retried by later ticks or reconciles
    h.engine.tick(h.clock.now())
    h.engine.reconcile(h.clock.now())
    assert h.sim.submitted == []


def test_short_entry_already_crossed_is_skipped_not_market(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.engine.quote_source = Quotes(AAA=(49.95, 50.05))          # bid already below the short trigger
    t = h.enter("AAA", -1, 100, entry="50.00", stop="50.20")
    assert t.state == "entry_crossed" and h.sim.submitted == []
    cand = [c for c in h.j.candidates(h.day) if c["symbol"] == "AAA"][0]
    assert cand["reason"] == "crossed_before_submit"


def test_trigger_needs_one_cent_clearance_like_alpaca(tmp_path):
    for ask, expect in (("49.99", "entry_open"), ("50.00", "entry_crossed")):
        h = Harness(tmp_path / ask)
        h.open_session()
        h.engine.quote_source = Quotes(AAA=(49.90, ask))
        assert h.enter("AAA", 1, 100, entry="50.00", stop="49.80").state == expect


def test_trigger_is_rounded_to_cents_before_comparing_and_submitting(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.engine.quote_source = Quotes(AAA=(49.90, 49.99))
    t = h.enter("AAA", 1, 100, entry="50.004", stop="49.80")    # rounds to 50.00 -> valid (49.99 + 0.01)
    assert t.state == "entry_open"
    assert h.order(t.entry_order_id).stop_price == D("50.00")


def test_crossed_entry_does_not_use_up_buying_power(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.engine.quote_source = Quotes(AAA=(50.05, 50.10), BBB=(49.40, 49.50))
    h.j.save_candidates(h.day, [{"symbol": "AAA", "rank": 1, "status": "candidate"},
                                {"symbol": "BBB", "rank": 2, "status": "candidate"}])
    plans = [h.plan("AAA", 1, 100), h.plan("BBB", 1, 100)]       # each needs 5,000
    assets = {s: {"tradable": True, "shortable": True, "easy_to_borrow": True} for s in ("AAA", "BBB")}
    h.engine.place_entries(plans, assets, {"buying_power": "5000", "shorting_enabled": True}, h.clock.now())
    assert h.trade("AAA").state == "entry_crossed" and h.trade("BBB").state == "entry_open"


def test_quote_valid_but_alpaca_422_crossed_is_skipped_exactly_once(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.engine.quote_source = Quotes(AAA=(49.80, 49.90))          # valid when checked ...
    h.sim.quote_fn = lambda sym: Quote(sym, D("50.00"), D("50.10"))   # ... crossed when Alpaca sees it
    t = h.enter("AAA", 1, 100, entry="50.00", stop="49.80")
    assert h.sim.crossed_rejections == 1 and len(entry_orders_sent(h)) == 1    # one attempt, one 422
    assert t.state == "entry_crossed"
    cand = [c for c in h.j.candidates(h.day) if c["symbol"] == "AAA"][0]
    assert cand["status"] == "skipped" and cand["reason"] == "crossed_before_submit"
    assert h.engine.stats["entry_crossed_422"] == 1 and h.engine.stats["entry_crossed_skipped"] == 1
    h.engine.tick(h.clock.now())
    h.engine.reconcile(h.clock.now())
    h.engine.recover(h.clock.now())                              # a restart does not retry it either
    assert len(entry_orders_sent(h)) == 1 and h.sim.crossed_rejections == 1
    assert not [r for r in h.sim.submitted if r.type == "market"]


def test_wide_iex_spread_fails_closed_no_order_is_sent(tmp_path):
    """2026-09-30: KOD (IEX 96.62 / 111.67) and DG (114.95 / 130.52) have spreads of 14%: that quote cannot judge the
    trigger. The entry is NOT sent (Alpaca is not asked), the signal is skipped for the day."""
    for sym, bid, ask, direction, entry, stop in (("KOD", 96.62, 111.67, 1, "97.88", "97.19"),
                                                   ("DG", 114.95, 130.52, -1, "122.81", "123.17")):
        h = Harness(tmp_path / sym)
        h.open_session()
        h.engine.quote_source = Quotes(**{sym: (bid, ask)})
        t = h.enter(sym, direction, 100, entry=entry, stop=stop)
        assert t.state == "not_submitted" and t.note == "skip_spread_too_wide"
        assert h.sim.submitted == [] and h.engine.stats["spread_too_wide_skipped"] == 1


def test_quotes_are_fetched_once_for_the_whole_batch(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    quotes = Quotes(AAA=(49.80, 49.90), BBB=(49.80, 49.90), CCC=(49.80, 49.90))
    h.engine.quote_source = quotes
    h.j.save_candidates(h.day, [{"symbol": s, "rank": i + 1, "status": "candidate"}
                                for i, s in enumerate(("AAA", "BBB", "CCC"))])
    assets = {s: {"tradable": True, "shortable": True, "easy_to_borrow": True} for s in ("AAA", "BBB", "CCC")}
    h.engine.place_entries([h.plan(s, 1, 100) for s in ("AAA", "BBB", "CCC")], assets,
                           {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    assert len(quotes.calls) == 1 and quotes.calls[0] == ["AAA", "BBB", "CCC"]     # one request, not one per symbol


def test_other_rejections_are_not_mistaken_for_crossed(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.engine.quote_source = Quotes(AAA=(49.80, 49.90))
    h.sim.reject_roles = {"entry"}                               # a 403, not a crossed 422
    t = h.enter("AAA", 1, 100)
    assert t.state == "entry_rejected" and h.engine.stats["entry_crossed_skipped"] == 0


def test_missing_or_unusable_quote_fails_closed_no_order_is_sent(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.engine.quote_source = Quotes(AAA=(49.80, None))            # no ask on the IEX book
    assert h.enter("AAA", 1, 100).state == "not_submitted" and h.sim.submitted == []

    h2 = Harness(tmp_path / "b")
    h2.open_session()
    h2.engine.quote_source = lambda symbols, now: (_ for _ in ()).throw(ConnectionError("quote API down"))
    assert h2.enter("AAA", 1, 100).state == "not_submitted"      # Alpaca's own 422 is NOT the fallback
    assert h2.sim.submitted == []


def test_error_classifiers():
    assert is_crossed_stop_rejection(OrderRejected("HTTP 422: stop price must be >= base price + 0.01", 422))
    assert not is_crossed_stop_rejection(OrderRejected("HTTP 403: insufficient buying power", 403))
    assert not is_crossed_stop_rejection(OrderRejected("HTTP 422: client_order_id must be unique", 422))
    assert is_not_replaceable_rejection(OrderRejected("HTTP 422: cannot replace order in pending_new", 422))
    assert not is_not_replaceable_rejection(OrderRejected("HTTP 403: forbidden", 403))


def test_parse_quote_treats_zero_side_as_missing():
    q = parse_quote("MSFT", {"ap": 0, "bp": 485.66, "t": "2026-09-30T20:00:02.817956342Z"})
    assert q.ask is None and q.bid == D("485.66") and q.t.year == 2026


# --- 2. protective stop: replaceable states only, one at a time --------------------------

def fill(h, qty, price="50.00", symbol="AAA"):
    h.sim.fill(h.trade(symbol).entry_order_id, D(qty), D(price))


def test_stop_in_pending_new_is_never_replaced(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    h.sim.initial_status = "pending_new"                         # Alpaca has accepted but not yet activated it
    fill(h, 30)
    h.pump()
    first = live_stops(h)[0]
    assert first.status == "pending_new" and first.qty == D(30)
    fill(h, 50)                                                  # more shares while the stop is pending_new
    h.pump()
    h.engine.tick(h.clock.now())
    assert stop_replaces(h) == []                                # no PATCH was sent
    assert h.sim.replace_rejected_state == 0                     # so Alpaca never said "cannot replace"
    assert live_stops(h)[0].order_id != first.order_id and live_stops(h)[0].qty == D(80)
    assert h.j.order(first.order_id).status == "canceled"
    assert h.trade().protected_qty == 0


def test_desired_stop_update_is_applied_once_status_becomes_new(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    h.sim.initial_status = "pending_new"
    fill(h, 30)
    h.pump()
    fill(h, 50)
    h.pump()
    assert stop_replaces(h) == []
    h.sim.activate()                                             # pending_new -> new
    h.pump()
    assert len(stop_replaces(h)) == 0                            # queued change applied exactly once
    assert h.sim.replace_rejected_state == 0
    stop = live_stops(h)[0]
    assert stop.qty == D(80) and h.trade().protected_qty == D(80)
    h.sim.activate()
    h.pump()
    h.engine.tick(h.clock.now())
    assert len(stop_replaces(h)) == 0                            # and not again


def test_multiple_partial_fills_do_not_create_duplicate_stops(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    fill(h, 30)
    fill(h, 50)
    fill(h, 20)                                                  # a burst: three fills, one drain
    h.pump()
    assert len(live_stops(h)) == 1 and live_stops(h)[0].qty == D(100)
    assert len(stop_replaces(h)) == 0                            # coalesced into ONE resize
    assert h.sim.max_live_stops["AAA"] == 1                      # never two live stops at once
    assert len(h.orders_by_role("stop")) == 1                    # original + one replacement
    assert h.engine.stats["duplicate_stops_canceled"] == 0


def test_partial_fills_with_pending_stop_end_with_one_full_stop(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    h.sim.initial_status = "pending_new"
    for qty in (30, 50, 20):
        fill(h, qty)
        h.pump()
    for _ in range(3):
        h.sim.activate()
        h.pump()
    assert len(live_stops(h)) == 1 and live_stops(h)[0].qty == D(100)
    assert h.sim.max_live_stops["AAA"] == 1 and h.sim.replace_rejected_state == 0


def test_stop_uses_alpaca_position_not_local_fill_sum(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    fill(h, 100)
    h.pump()
    assert live_stops(h)[0].qty == D(100)
    h.sim.positions["AAA"] = D(60)                               # someone sold 40 by hand
    h.engine._stop_dirty.add("AAA")
    h.engine.flush_stops()
    assert live_stops(h)[0].qty == D(60)                        # a smaller position must persist first ...
    h.clock.sleep(5)
    h.engine.tick(h.clock.now())
    assert live_stops(h)[0].qty == D(60)                        # (a fill under 15 s old: /positions may just lag)
    h.clock.sleep(20)
    h.engine.tick(h.clock.now())                                 # first sighting of the smaller position ...
    h.engine.tick(h.clock.now())
    assert live_stops(h)[0].qty == D(60)
    h.clock.sleep(5)
    h.engine.tick(h.clock.now())                                 # ... confirmed 3+ s later
    h.engine.tick(h.clock.now())
    h.pump()
    assert live_stops(h)[0].qty == D(60)                         # the stop follows Alpaca's quantity


def test_stale_not_replaceable_422_is_deferred_not_a_flatten(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    fill(h, 30)
    h.pump()
    stop = live_stops(h)[0]
    h.sim.orders[stop.order_id].status = "pending_replace"       # Alpaca moved on; our view still says `new`
    fill(h, 20)
    h.pump()
    assert h.sim.replace_rejected_state == 0 and stop_replaces(h) == []
    assert h.orders_by_role("stopupd") == [] and h.orders_by_role("stopfail") == []     # nobody flattened
    h.sim.orders[stop.order_id].status = "new"
    h.engine._stop_dirty.add("AAA")
    h.pump()
    assert live_stops(h)[0].qty == D(50)


def test_a_second_live_stop_is_canceled(tmp_path):
    from orb_iex.orders import OrderRequest
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    fill(h, 100)
    h.pump()
    h.sim.submit_order(OrderRequest("AAA", "sell", D(100), "stop", "orbiex-20260924-AAA-stop9", D("49.80")))
    h.pump()
    h.engine._stop_dirty.add("AAA")
    h.engine.flush_stops()
    h.pump()
    assert len(live_stops(h)) == 1 and h.engine.stats["duplicate_stops_canceled"] == 1


# --- 3. protective stop safety --------------------------------------------------------------

def test_crossed_protective_stop_is_never_skipped_it_flattens(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    h.sim.last_price["AAA"] = D("49.70")
    h.sim.auto_fill_market = True
    h.sim.check_protective_stops = True
    h.sim.quote_fn = lambda sym: Quote(sym, D("49.70"), D("49.75"))   # price already through the 49.80 stop
    fill(h, 100)
    h.pump()
    t = h.trade()
    assert t.closed and t.state == "stop_failed"
    assert h.sim.crossed_rejections == 0 and h.engine.stats["protective_stop_crossed"] == 1
    assert len([r for r in h.sim.submitted if r.client_order_id.endswith("-stop1")]) == 0    # not retried
    exits = h.orders_by_role("stopfail")
    assert len(exits) == 1 and exits[0].qty == D(100) and exits[0].type == "market"
    h.pump()
    assert h.sim.list_positions() == {}
    assert h.order(t.entry_order_id).status in ("canceled", "filled")


def test_crossed_short_protective_stop_flattens(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter("AAA", -1, 100, entry="50.00", stop="50.20")
    h.sim.last_price["AAA"] = D("50.30")
    h.sim.auto_fill_market = True
    h.sim.check_protective_stops = True
    h.sim.quote_fn = lambda sym: Quote(sym, D("50.25"), D("50.30"))
    h.sim.fill(h.trade().entry_order_id, D(100), D("50.00"))
    h.pump()
    assert h.trade().state == "stop_failed" and len(h.orders_by_role("stopfail")) == 1
    h.pump()
    assert h.sim.list_positions() == {}


# --- 4. idempotency and monotonic state -------------------------------------------------------

def test_duplicate_websocket_fill_does_not_double_count(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(40), D("50.00"))
    events = []
    while not h.q.empty():
        events.append(h.q.get())
    for u in events * 3:
        h.engine.on_update(u)
    h.engine.flush_stops()
    assert h.j.owned_qty()["AAA"] == D(40)
    assert len(h.j.fills(h.day.isoformat())) == 1
    assert len(live_stops(h)) == 1 and live_stops(h)[0].qty == D(40) and stop_replaces(h) == []
    assert h.engine.stats["duplicates_ignored"] >= 1


def test_old_rest_snapshot_never_overwrites_newer_state(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D("50.00"))
    h.pump()
    entry_id = t.entry_order_id
    stale = replace(h.sim.orders[entry_id], status="new", filled_qty=D(0), filled_avg_price=None,
                    source="rest", event=None, timestamp=(h.clock.now() - dt.timedelta(seconds=30)).isoformat())
    h.engine.on_update(stale)
    rec = h.j.order(entry_id)
    assert rec.status == "filled" and rec.filled_qty == D(100)   # terminal/fill state never goes back
    assert h.j.owned_qty()["AAA"] == D(100)


def test_old_snapshot_cannot_move_a_live_stop_back_to_pending(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    fill(h, 100)
    h.pump()
    stop = live_stops(h)[0]
    old_ts = (h.clock.now() - dt.timedelta(seconds=30)).isoformat()
    h.engine._seen_ts[stop.order_id] = h.clock.now()
    h.engine.on_update(replace(h.sim.orders[stop.order_id], status="pending_new", source="rest", timestamp=old_ts))
    assert h.j.order(stop.order_id).status == "new"
    assert h.engine.stats["stale_snapshots_ignored"] == 1


def test_lost_stop_reply_retries_the_same_id_and_never_duplicates(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter()
    h.sim.timeout_roles = {"stop"}                               # request reaches Alpaca, the reply is lost
    fill(h, 100)
    h.pump()
    assert len(h.sim.orders) == 2                                # entry + the one stop Alpaca really has
    h.engine.tick(h.clock.now())                                 # retry with the same client_order_id
    h.pump()
    stops = [o for o in h.sim.orders.values() if o.client_order_id.endswith("-stop1")]
    assert len(stops) == 1 and h.sim.max_live_stops["AAA"] == 1
    assert h.trade().stop_order_id == stops[0].order_id and h.trade().state == "active"
    assert h.orders_by_role("stopfail") == []                    # no panic flatten for a lost reply


def test_lost_entry_reply_is_not_resubmitted(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.sim.timeout_roles = {"entry"}
    t = h.enter()
    assert t.state == "entry_open" and h.engine.stats["entry_unconfirmed"] == 1
    h.engine.reconcile(h.clock.now())                            # REST list adopts the order Alpaca has
    h.pump()
    assert len(entry_orders_sent(h)) == 1 and len(h.sim.orders) == 1
    assert h.trade().entry_order_id == next(iter(h.sim.orders))


# --- 5. user-owned orders and positions ------------------------------------------------------------

def test_user_orders_and_positions_untouched_through_crossed_and_stop_changes(tmp_path):
    h = Harness(tmp_path, now=et(dt.date(2026, 9, 24), 15, 55))
    h.open_session()
    manual = unrelated_order(h.sim, "MANUAL")
    h.sim.positions["USER"] = D(25)
    h.engine.quote_source = Quotes(AAA=(49.5, 49.9), BBB=(50.05, 50.10))
    h.j.save_candidates(h.day, [{"symbol": s, "rank": i + 1, "status": "candidate"}
                                for i, s in enumerate(("AAA", "BBB"))])
    assets = {s: {"tradable": True, "shortable": True, "easy_to_borrow": True} for s in ("AAA", "BBB")}
    h.engine.place_entries([h.plan("AAA", 1, 100), h.plan("BBB", 1, 100)], assets,
                           {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    assert h.trade("BBB").state == "entry_crossed"
    h.sim.positions["AAA"] = D(50)                               # the user also bought 50 AAA by hand
    fill(h, 30)
    fill(h, 70)
    h.pump()
    assert live_stops(h)[0].qty == D(100)                        # the bot's shares only, not 150
    h.engine.begin_eod(h.clock.now())
    h.pump()
    h.engine.tick(h.clock.now())
    assert h.sim.orders[manual.order_id].status == "new"
    touched = {w[1] for w in h.sim.write_log if w[0] in ("cancel", "replace")}
    assert manual.order_id not in touched
    assert h.sim.positions["USER"] == D(25)
    assert all(r.client_order_id.startswith("orbiex-") for r in h.sim.submitted if r.symbol != "MANUAL")


# === 2026-09-30 real-log reproductions =====================================================================

def run_ticks(h, seconds, step=2):
    """What the runner does while the market is open: advance time, tick, drain events."""
    for _ in range(int(seconds / step)):
        h.clock.sleep(step)
        h.engine.tick(h.clock.now())
        h.pump()


def short_trade(h, symbol, qty=100, entry="10.44", stop="10.52"):
    h.j.save_candidates(h.day, [{"symbol": symbol, "rank": 1, "status": "candidate"}])
    h.engine.place_entries([h.plan(symbol, -1, qty, entry=entry, stop=stop)],
                           {symbol: {"tradable": True, "shortable": True, "easy_to_borrow": True}},
                           {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    h.pump()
    return h.trade(symbol)


def test_trlv_cnk_rejected_protective_stop_flattens_at_once_and_keeps_trying(tmp_path):
    """TRLV-stop1 / CNK-stop1: 'stop price must be greater than current price'. The buy stop is 8-11 cents
    above the fill, inside the spread of a $10-$38 stock, so it is already crossed when placed. The position
    is flattened at once; even when the first exits are refused, and /positions lags the fill, it keeps
    going until flat (no 5-attempt limit)."""
    for symbol, entry, stop in (("TRLV", "10.44", "10.52"), ("CNK", "38.12", "38.23")):
        h = Harness(tmp_path / symbol)
        h.open_session()
        t = short_trade(h, symbol, 100, entry, stop)
        h.sim.last_price[symbol] = D(entry)
        h.sim.auto_fill_market = True
        h.sim.check_protective_stops = True
        h.sim.quote_fn = lambda sym, a=D(entry) * D("1.10"): Quote(sym, a - D("0.05"), a)   # ask is above the stop
        h.sim.reject_role_counts = {"stopfail": 8}        # the cover order is refused 8 times
        real_positions = h.sim.list_positions
        h.sim.list_positions = lambda: {}                                # /positions has not caught up yet
        h.sim.fill(t.entry_order_id, D(100), D(entry))
        h.pump()
        tr = h.trade(symbol)
        assert h.j.breakers() and not h.orders_by_role("stop", symbol)
        assert len([r for r in h.sim.submitted if r.client_order_id.endswith("-stop1")]) == 0   # stop not retried
        h.clock.sleep(16)                                                # the lag window is over
        h.sim.list_positions = real_positions
        run_ticks(h, 50)
        assert h.sim.positions == {}, f"{symbol} still held"
        assert not h.engine.open_orders(symbol)
        assert h.sim.reject_role_counts == {"stopfail": 0}  # all 8 refusals were retried, past the old cap of 5
        assert len(h.orders_by_role("stopfail", symbol)) == 1             # and only the accepted one is an order


def test_position_guard_flattens_a_closed_trade_whose_flatten_was_lost(tmp_path):
    """A rejected stop closes the trade; if the exit never happens (event lost, /positions lag) the guard
    still finds the shares and flattens them."""
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50))
    h.pump()
    tr = h.trade()
    tr.closed, tr.state = True, "stop_failed"           # closed, but no exit was ever placed
    h.j.save_trade(tr)
    h.sim.cancel_order(live_stops(h)[0].order_id)
    h.pump()
    h.sim.auto_fill_market, h.sim.last_price["AAA"] = True, D(50)
    run_ticks(h, 40)
    assert h.sim.positions == {} and h.engine.stats["position_guard_actions"] >= 1
    exits = h.orders_by_role('flatten')
    assert len(exits) == 2 and exits[0].status == 'canceled' and exits[1].status == 'filled'


def test_position_guard_places_a_missing_stop(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50))
    h.pump()
    stop = live_stops(h)[0]
    h.sim.orders[stop.order_id].status = "canceled"      # vanished at the broker without a usable event
    rec = h.j.order(stop.order_id)
    rec.status, rec.cancel_requested = "canceled", True  # the journal saw it as our own cancel
    h.j.save_order(rec)
    run_ticks(h, 4)
    assert len(live_stops(h)) == 1 and live_stops(h)[0].qty == D(100)
    assert h.engine.stats["position_guard_actions"] == 1 and h.orders_by_role("flatten") == []


def test_kod_three_partial_fills_while_stop_pending_new_never_patch(tmp_path):
    """KOD: three 'HTTP 422: cannot replace order in pending_new status'. Three partial fills arrive while
    the protective stop is still pending_new: nothing is sent; one resize goes out once it is `new`."""
    h = Harness(tmp_path)
    h.open_session()
    h.enter("KOD", 1, 71, entry="97.88", stop="97.19")
    h.sim.initial_status = "pending_new"
    for qty in (20, 25, 26):
        h.sim.fill(h.trade("KOD").entry_order_id, D(qty), D("97.88"), emit=True)
        h.pump()
        run_ticks(h, 2)
    assert stop_replaces(h) == [] and h.sim.replace_rejected_state == 0
    assert live_stops(h, "KOD")[0].qty == D(71) and live_stops(h, "KOD")[0].status == "pending_new"
    h.sim.activate()
    h.pump()
    run_ticks(h, 2)
    assert len(stop_replaces(h)) == 0 and h.sim.replace_rejected_state == 0
    assert live_stops(h, "KOD")[0].qty == D(71) and len(live_stops(h, "KOD")) == 1
    assert h.sim.max_live_stops["KOD"] == 1


# --- CBRS: the short that stayed overnight --------------------------------------------------------------

def eod_harness(tmp_path, hh=15, mm=58, ss=50):
    h = Harness(tmp_path, now=et(dt.date(2026, 9, 24), hh, mm, ss))
    h.open_session()
    return h


def test_cbrs_cancel_that_never_takes_effect_then_late_fill_is_flattened(tmp_path):
    """CBRS (short 45): its trigger 177.86 was first touched at 15:59:25, after the EOD cancel at 15:59:00.
    Here the cancel is refused, then accepted but ignored, so the sell stop fills inside the EOD window; the
    late-fill exit is refused repeatedly. The position must still be flat before the 15:59:55 cutoff."""
    h = eod_harness(tmp_path)
    t = short_trade(h, "CBRS", 45, "177.86", "178.95")
    h.sim.last_price["CBRS"] = D("177.86")
    h.sim.auto_fill_market = True
    h.sim.cancel_fail_count = 2                           # the first EOD cancels are refused
    h.clock.sleep(10)                                     # 15:59:00 - EOD begins
    h.engine.begin_eod(h.clock.now())
    h.pump()
    assert h.order(t.entry_order_id).status == "new"      # still live at Alpaca
    h.sim.cancel_ignored.add(t.entry_order_id)            # then cancels are accepted but have no effect
    h.sim.reject_role_counts = {"lateexit": 1, "flatten": 4, "eod": 2}
    run_ticks(h, 20)                                      # up to 15:59:20
    h.sim.fill(t.entry_order_id, D(45), D("177.86"))      # 15:59:25-ish: the trigger prints, the stop fills
    h.pump()
    assert h.sim.positions == {"CBRS": D(-45)} or h.sim.positions == {}
    run_ticks(h, 26)                                      # to ~15:59:56
    assert h.sim.positions == {}, "CBRS must be flat before the close"
    assert h.engine.bot_positions() == {}
    assert h.engine.stats["eod_cancel_retries"] >= 1


def test_leftover_after_the_exit_cutoff_is_flattened_after_hours_and_verified(tmp_path):
    """Whatever is still held after 15:59:55 is closed after hours with an extended-hours marketable limit,
    and the day is not closed out until Alpaca confirms ORB-IEX is flat."""
    h = eod_harness(tmp_path)
    t = short_trade(h, "CBRS", 45, "177.86", "178.95")
    h.sim.last_price["CBRS"] = D("177.86")
    h.sim.cancel_ignored.add(t.entry_order_id)            # the EOD cancel never takes effect
    h.clock.sleep(10)
    h.engine.begin_eod(h.clock.now())
    h.clock.sleep(48)                                     # 15:59:48 ... the entry fills at 15:59:58
    h.sim.fill(t.entry_order_id, D(45), D("177.86"))
    h.clock.sleep(10)
    h.pump()
    assert h.sim.positions == {"CBRS": D(-45)}            # the exit was refused: "no new exits in the last 5 s"
    assert h.orders_by_role("lateexit", "CBRS") == []
    h.clock.sleep(5)                                      # 16:00:03: after the close
    assert h.engine.post_close_step(h.clock.now()) is False          # not flat: stays on it (waits for in-flight)
    h.clock.sleep(15)
    h.sim.auto_fill_market = False                        # the first limit rests
    assert h.engine.post_close_step(h.clock.now()) is False
    pc = h.orders_by_role("postclose", "CBRS")
    assert len(pc) == 1 and h.sim.submitted[-1].extended_hours and h.sim.submitted[-1].type == "limit"
    assert h.sim.submitted[-1].side == "buy" and h.sim.submitted[-1].limit_price > D("177.86")   # marketable buy
    first_limit = h.sim.submitted[-1].limit_price
    h.clock.sleep(25)                                     # still unfilled: widened (cancel, then a new limit)
    h.engine.post_close_step(h.clock.now())
    h.pump()
    h.sim.auto_fill_market = True
    h.clock.sleep(3)
    h.engine.post_close_step(h.clock.now())
    h.pump()
    h.engine.post_close_step(h.clock.now())
    h.pump()
    assert len(h.orders_by_role("postclose", "CBRS")) == 2
    assert h.sim.submitted[-1].limit_price > first_limit
    h.sim.last_price["CBRS"] = D("177.90")
    h.sim.fill(h.orders_by_role("postclose", "CBRS")[-1].order_id, D(45), D("177.90"))
    h.pump()
    assert h.sim.positions == {}
    assert h.engine.post_close_step(h.clock.now()) is True            # verified flat: the day may close out
    assert h.engine.entries_blocked is None


def test_nothing_fills_after_hours_fails_safe_and_blocks_entries(tmp_path):
    h = eod_harness(tmp_path)
    t = short_trade(h, "CBRS", 45, "177.86", "178.95")
    h.sim.last_price["CBRS"] = D("177.86")
    h.sim.auto_fill_market = False                        # the broker never fills anything
    h.sim.cancel_ignored.add(t.entry_order_id)
    h.clock.sleep(10)
    h.engine.begin_eod(h.clock.now())
    h.clock.sleep(58)
    h.sim.fill(t.entry_order_id, D(45), D("177.86"))
    h.pump()
    done = False
    while h.clock.now() < et(dt.date(2026, 9, 24), 20, 5):
        h.clock.sleep(10)
        h.pump()
        done = h.engine.post_close_step(h.clock.now())
        if done:
            break
    assert done and h.clock.now() >= et(dt.date(2026, 9, 24), 20, 0)    # only after extended hours ended
    assert h.engine.entries_blocked == "overnight_position"
    assert "CBRS" in h.j.get_meta("overnight_positions")
    limits = [r.limit_price for r in h.sim.submitted if r.client_order_id.split("-")[-1].startswith("postclose")]
    assert len(limits) >= 4 and limits == sorted(limits)               # each retry pushes the limit further through


def test_next_session_with_a_leftover_blocks_entries_until_it_is_flat(tmp_path):
    h = Harness(tmp_path, now=et(dt.date(2026, 9, 24), 9, 20))
    from helpers import bot_leftover
    bot_leftover(h.j, "OLD", -45)
    h.sim.positions["OLD"] = D(-45)
    h.sim.last_price["OLD"] = D(177)
    h.engine.recover(h.clock.now())
    assert h.engine.entries_blocked == "overnight_position"
    h.open_session()
    h.enter("NEW")                                        # a new entry while a leftover is still held
    assert h.trade("NEW") is None
    assert [c["reason"] for c in h.j.candidates(h.day) if c["symbol"] == "NEW"] == ["overnight_position"]
    h.sim.auto_fill_market = True
    h.clock.sleep(11 * 60)                                # 09:31: the open sweep covers it
    h.engine.sweep("opensweep", "Open sweep")
    h.pump()
    assert h.sim.positions == {}
    h.engine.tick(h.clock.now())
    assert h.engine.entries_blocked is None


def test_exit_conflict_breaker_records_the_quantities_that_disagreed(tmp_path):
    """USO 2026-10-26: a flatten was cancelled because /positions already showed its first partial fill while
    the journal did not. The breaker must say which numbers disagreed so this race can be proven from the log."""
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50))
    h.pump()
    tr = h.trade()
    tr.closed, tr.state = True, "stop_failed"
    h.j.save_trade(tr)
    h.sim.cancel_order(live_stops(h)[0].order_id)
    h.pump()
    run_ticks(h, 4)
    exit_order = [o for o in h.orders_by_role("flatten") if o.status not in ("canceled", "filled")][0]
    h.sim.positions["AAA"] = D(90)                      # broker sold 10; the order record has not heard yet
    h.engine._broker_snapshot_valid = False
    h.engine.reconcile_symbol("AAA")
    detail = {b[0]: b[2] for b in h.j.breakers()}["duplicate_order"]
    assert f"qty={exit_order.qty}" in detail and "filled=0" in detail and "position=90" in detail


def test_wide_spread_quote_does_not_flatten_a_healthy_position(tmp_path):
    """USO 2026-10-26: a junk IEX bid far below the ask read as a breached stop and flattened a healthy position.
    A spread wider than the configured limit cannot judge the stop; the stop must be placed and the broker decides."""
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.latest_quote = lambda symbol: {"t": h.clock.now().isoformat(), "bp": "49.00", "ap": "50.60"}   # 3.2% wide, bid under the stop
    h.sim.fill(t.entry_order_id, D(100), D(50))
    h.pump()
    assert live_stops(h) and live_stops(h)[0].qty == D(100)
    assert not [o for o in h.orders_by_role("flatten") if o.status not in ("canceled", "rejected")]


def test_tight_quote_through_the_stop_still_flattens(tmp_path):
    """The wide-spread exemption must not weaken the real check: a tight quote through the stop is still breached."""
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.latest_quote = lambda symbol: {"t": h.clock.now().isoformat(), "bp": "49.50", "ap": "49.52"}
    h.sim.fill(t.entry_order_id, D(100), D(50))
    h.pump()
    assert not live_stops(h)
    exits = [o for o in h.j.orders(h.day.isoformat(), "AAA") if o.role not in ("entry", "stop") and o.status != "rejected"]
    assert exits and exits[0].qty == D(100)


def _filled_without_event(h, order_id, qty, price):
    """The broker filled the order, /positions shows it, but the journal has not heard yet (USO, RPRX, CLF)."""
    h.sim.fill(order_id, D(qty), D(price), emit=False)
    h.engine._positions_snapshot = h.sim.list_positions()
    h.engine._broker_snapshot_valid = True


def _live_flatten(h):
    h.sim.positions["AAA"] = D(100)
    t = h.trade()
    t.closed, t.state = True, "stop_failed"
    h.j.save_trade(t)
    h.sim.cancel_order(live_stops(h)[0].order_id)
    h.pump()
    run_ticks(h, 4)
    return [o for o in h.orders_by_role("flatten") if o.status not in ("canceled", "filled")][0]


def test_partial_exit_fill_seen_only_in_positions_is_not_a_conflict(tmp_path):
    """USO 2026-10-26: /positions showed a flatten's first partial fill before the order record did. The engine must
    re-read the broker, find them consistent, and leave the exit alone instead of cancelling and re-sending it."""
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50))
    h.pump()
    exit_order = _live_flatten(h)
    _filled_without_event(h, exit_order.order_id, 10, 49.9)
    h.engine.reconcile_symbol("AAA")
    assert "duplicate_order" not in {b[0] for b in h.j.breakers()}
    assert h.j.order(exit_order.order_id).status != "canceled"
    assert h.j.order(exit_order.order_id).filled_qty == D(10)
    assert not [w for w in h.sim.write_log if w[0] == "cancel" and w[1] == exit_order.order_id]


def test_exit_filled_in_full_seen_only_in_positions_is_not_a_mismatch(tmp_path):
    """RPRX 2026-10-21, CLF 2026-10-19: the broker was already flat while the journal still owned the shares. The
    engine must re-read the broker, not cancel an order that is already filled (HTTP 422) or raise a breaker."""
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50))
    h.pump()
    exit_order = _live_flatten(h)
    _filled_without_event(h, exit_order.order_id, 100, 49.9)
    h.engine.reconcile_symbol("AAA")
    assert "reconciliation_unhealthy" not in {b[0] for b in h.j.breakers()}
    assert h.j.order(exit_order.order_id).status == "filled"
    assert not [w for w in h.sim.write_log if w[0] == "cancel" and w[1] == exit_order.order_id]


def test_a_real_exit_quantity_conflict_still_raises_the_breaker(tmp_path):
    """The re-read must not hide a genuine disagreement: the broker itself says the exit is the wrong size."""
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50))
    h.pump()
    exit_order = _live_flatten(h)
    h.sim.positions["AAA"] = D(90)                      # nothing the order list can explain
    h.engine._positions_snapshot = h.sim.list_positions()
    h.engine._broker_snapshot_valid = True
    h.engine.reconcile_symbol("AAA")
    assert "duplicate_order" in {b[0] for b in h.j.breakers()}
