"""The entry quote gate FAILS CLOSED (P0-A final decisions): when a usable quote cannot be obtained before an entry,
no stop order is sent to Alpaca, the signal is skipped for the day and the reason is journaled
("skip_quote_unavailable"). Alpaca's own order validation is not the fallback for missing market data."""

import datetime as dt
import json
from dataclasses import replace
from decimal import Decimal as D

import pytest

from helpers import Harness
from orb_iex.marketdata import Quote
from orb_iex.tradelog import TradeLog


class Quotes:
    def __init__(self, age=0.3, **q):
        self.age = age
        self.q = {s: Quote(s, None if b is None else D(str(b)), None if a is None else D(str(a))) for s, (b, a) in q.items()}
        self.calls = []

    def __call__(self, symbols, now):
        self.calls.append((list(symbols), now))
        return {s: replace(self.q[s], t=now - dt.timedelta(seconds=self.age)) for s in symbols if s in self.q}


def harness(tmp_path, **over):
    h = Harness(tmp_path, **over)
    h.open_session()
    h.engine.tlog = TradeLog(tmp_path / "tl.sqlite", tmp_path / "exports")
    return h


def enter(h, source, symbol="AAA", direction=1, **kw):
    h.engine.quote_source = source
    h.j.save_candidates(h.day, [{"symbol": symbol, "rank": 1, "status": "candidate"}])
    h.engine.place_entries([h.plan(symbol, direction, 100, **kw)],
                           {symbol: {"tradable": True, "shortable": True, "easy_to_borrow": True}},
                           {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    return h.trade(symbol)


def journal(h, kind, sym="AAA"):
    out = []
    for e in h.engine.tlog.events(h.day.isoformat(), sym):
        if e["kind"] == kind:
            d = e["detail"]
            out.append({**e, "detail": d if isinstance(d, dict) else json.loads(d)})
    return out


def assert_failed_closed(h, t, cause=None, sym="AAA"):
    assert t.state == "not_submitted" and t.note == "skip_quote_unavailable" and t.entry_order_id is None
    assert h.sim.submitted == [] and h.sim.orders == {}               # NOTHING was sent to Alpaca
    c = [x for x in h.j.candidates(h.day) if x["symbol"] == sym][0]
    assert c["status"] == "skipped" and c["reason"] == "skip_quote_unavailable"
    skip = journal(h, "skip", sym)
    assert len(skip) == 1 and skip[0]["detail"]["reason"] == "skip_quote_unavailable"
    q = journal(h, "entry_quote", sym)
    assert len(q) == 1 and q[0]["status"] == "skip_quote_unavailable"
    if cause:
        assert q[0]["detail"]["cause"] == cause and skip[0]["detail"]["cause"] == cause
    for k in ("quote_exchange_time", "received_at", "quote_age_seconds", "bid", "ask", "spread", "feed",
              "max_quote_age_seconds"):
        assert k in skip[0]["detail"], k                              # the journal keeps the shape even with no data


# --- every way a quote can be missing ------------------------------------------------------------------------------------

def test_no_quote_source_at_all_fails_closed(tmp_path):
    h = harness(tmp_path)
    assert_failed_closed(h, enter(h, None), "no_quote_source")


def test_source_returns_nothing_for_the_symbol_fails_closed(tmp_path):
    h = harness(tmp_path)
    assert_failed_closed(h, enter(h, Quotes()), "no_quote_for_symbol")


def test_quote_request_error_fails_closed_and_does_not_fall_back_to_alpaca(tmp_path):
    h = harness(tmp_path)

    def down(symbols, now):
        raise ConnectionError("quote API down")
    t = enter(h, down)
    assert t.state == "not_submitted" and h.sim.submitted == []
    assert "fetch_failed" in journal(h, "entry_quote")[0]["detail"]["cause"]
    assert [c["reason"] for c in h.j.candidates(h.day) if c["symbol"] == "AAA"] == ["skip_quote_unavailable"]


@pytest.mark.parametrize("bid, ask, cause", [(49.80, None, "one_sided_quote"), (None, 49.90, "one_sided_quote"),
                                             (49.90, 49.90, "locked_or_crossed_book"),
                                             (49.95, 49.90, "locked_or_crossed_book")])
def test_one_sided_or_locked_book_fails_closed(tmp_path, bid, ask, cause):
    h = harness(tmp_path)
    assert_failed_closed(h, enter(h, Quotes(AAA=(bid, ask))), cause)


def test_a_failed_batch_skips_every_entry_and_asks_once(tmp_path):
    h = harness(tmp_path)
    calls = []

    def down(symbols, now):
        calls.append(symbols)
        raise TimeoutError("timed out")
    h.engine.quote_source = down
    h.j.save_candidates(h.day, [{"symbol": s, "rank": i + 1, "status": "candidate"} for i, s in enumerate(("AAA", "BBB", "CCC"))])
    assets = {s: {"tradable": True, "shortable": True, "easy_to_borrow": True} for s in ("AAA", "BBB", "CCC")}
    h.engine.place_entries([h.plan(s, 1, 100) for s in ("AAA", "BBB", "CCC")], assets,
                           {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    assert h.sim.submitted == [] and len(calls) == 1                  # one failure, no retry storm per symbol
    assert {h.trade(s).state for s in ("AAA", "BBB", "CCC")} == {"not_submitted"}


def test_a_signal_skipped_for_a_missing_quote_is_never_retried_that_day(tmp_path):
    h = harness(tmp_path)
    t = enter(h, Quotes())
    good = Quotes(AAA=(49.80, 49.90))
    h.engine.quote_source = good                                       # the data comes back later
    h.engine.submit_entry(h.trade("AAA"))                              # a direct retry is refused
    h.engine.tick(h.clock.now())
    h.engine.recover(h.clock.now())                                    # and so is a restart
    h.engine.place_entries([h.plan("AAA", 1, 100)], {"AAA": {"tradable": True, "shortable": True,
                                                           "easy_to_borrow": True}},
                           {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    assert h.sim.submitted == [] and h.trade("AAA").state == "not_submitted"


def test_a_missing_quote_frees_its_buying_power_for_the_next_entry(tmp_path):
    h = harness(tmp_path)
    q = Quotes(BBB=(49.80, 49.90))                                     # AAA has no quote, BBB does
    h.engine.quote_source = q
    h.j.save_candidates(h.day, [{"symbol": "AAA", "rank": 1, "status": "candidate"},
                                {"symbol": "BBB", "rank": 2, "status": "candidate"}])
    assets = {s: {"tradable": True, "shortable": True, "easy_to_borrow": True} for s in ("AAA", "BBB")}
    h.engine.place_entries([h.plan("AAA", 1, 100), h.plan("BBB", 1, 100)], assets,
                           {"buying_power": "5000", "shorting_enabled": True}, h.clock.now())   # room for ONE
    assert h.trade("AAA").state == "not_submitted" and h.trade("BBB").state == "entry_open"


# --- age: default 2 s, configurable, journaled with every field ----------------------------------------------------------------

def test_default_age_limit_is_two_seconds_and_configurable(tmp_path):
    assert harness(tmp_path).settings.max_quote_age_seconds == 2
    h = harness(tmp_path / "a")
    assert enter(h, Quotes(age=1.9, AAA=(49.80, 49.90))).state == "entry_open"
    h2 = harness(tmp_path / "b")
    t = enter(h2, Quotes(age=2.5, AAA=(49.80, 49.90)))
    assert t.state == "not_submitted" and t.note == "stale_quote" and h2.sim.submitted == []
    h3 = harness(tmp_path / "c", trading__max_quote_age_seconds=5)
    assert enter(h3, Quotes(age=4.0, AAA=(49.80, 49.90))).state == "entry_open"


def test_every_decision_is_journaled_with_all_quote_fields(tmp_path):
    h = harness(tmp_path)
    enter(h, Quotes(age=1.0, AAA=(49.80, 49.90)))
    d = journal(h, "entry_quote")[0]["detail"]
    assert d["bid"] == "49.8" and d["ask"] == "49.9" and d["spread"] == "0.1" and d["spread_pct"] == "0.201"
    assert d["feed"] == "iex" and d["quote_age_seconds"] == 1.0 and d["max_quote_age_seconds"] == 2
    assert d["quote_exchange_time"] and d["received_at"] and d["decision_time"]


def test_a_late_symbol_in_a_long_batch_gets_a_fresh_quote_instead_of_going_stale(tmp_path):
    """With a 2 s limit a batch quote is too old after ~1 s of order sending. A later symbol is re-read, so slow
    order submission does not turn good signals into stale skips."""
    h = harness(tmp_path)
    q = Quotes(age=0.3, AAA=(49.80, 49.90), BBB=(49.80, 49.90), CCC=(49.80, 49.90))
    h.engine.quote_source = q
    real = h.sim.submit_order

    def slow_submit(req):                                              # each order takes 0.9 s to send
        out = real(req)
        h.clock.sleep(0.9)
        return out
    h.sim.submit_order = slow_submit
    h.j.save_candidates(h.day, [{"symbol": s, "rank": i + 1, "status": "candidate"} for i, s in enumerate(("AAA", "BBB", "CCC"))])
    assets = {s: {"tradable": True, "shortable": True, "easy_to_borrow": True} for s in ("AAA", "BBB", "CCC")}
    h.engine.place_entries([h.plan(s, 1, 100) for s in ("AAA", "BBB", "CCC")], assets,
                           {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    assert {h.trade(s).state for s in ("AAA", "BBB", "CCC")} == {"entry_open"}
    assert len(q.calls) > 1 and q.calls[0][0] == ["AAA", "BBB", "CCC"]    # one batch, then per-symbol re-reads
    for s in ("AAA", "BBB", "CCC"):
        assert journal(h, "entry_quote", s)[0]["detail"]["quote_age_seconds"] <= 2


# --- spread gate: FAIL CLOSED, default 1%, configurable ---------------------------------------------------------------------

def test_wide_spread_fails_closed_and_journals_every_field(tmp_path):
    h = harness(tmp_path)
    t = enter(h, Quotes(age=0.5, KOD=(96.62, 111.67)), symbol="KOD", entry="97.88", stop="97.19")
    assert t.state == "not_submitted" and t.note == "skip_spread_too_wide" and t.entry_order_id is None
    assert h.sim.submitted == [] and h.sim.orders == {}                       # nothing reached Alpaca
    c = [x for x in h.j.candidates(h.day) if x["symbol"] == "KOD"][0]
    assert c["status"] == "skipped" and c["reason"] == "skip_spread_too_wide"
    skip, quote = journal(h, "skip", "KOD"), journal(h, "entry_quote", "KOD")
    assert len(skip) == 1 and skip[0]["detail"]["reason"] == "skip_spread_too_wide"
    assert quote[0]["status"] == "skip_spread_too_wide"
    for d in (skip[0]["detail"], quote[0]["detail"]):
        assert d["bid"] == "96.62" and d["ask"] == "111.67" and d["spread"] == "15.05"
        assert d["quote_age_seconds"] == 0.5 and d["feed"] == "iex" and d["max_quote_spread_pct"] == "1"
        assert d["spread_pct"] == "14.451"
    assert h.engine.stats["spread_too_wide_skipped"] == 1


def test_default_threshold_is_one_percent_and_configurable(tmp_path):
    assert harness(tmp_path).settings.max_quote_spread_pct == D("1")
    h = harness(tmp_path / "a")
    assert enter(h, Quotes(AAA=(49.70, 50.20))).state == "not_submitted"        # 1.0% + a hair: skipped (trigger 50)
    h2 = harness(tmp_path / "b")
    assert enter(h2, Quotes(AAA=(49.70, 49.90))).state == "entry_open"           # 0.40%: fine
    h3 = harness(tmp_path / "c", trading__max_quote_spread_pct="20")
    t3 = enter(h3, Quotes(KOD=(96.62, 111.67)), symbol="KOD", entry="97.88", stop="97.19")
    assert t3.note == "crossed_before_submit"           # the spread gate let it through; the next check (ask 111.67 > trigger) decides
    for bad in (0, -1, 101, "x", True):
        with pytest.raises(Exception):
            Harness(tmp_path / "bad", trading__max_quote_spread_pct=bad)


def test_exactly_at_the_threshold_is_allowed(tmp_path):
    h = harness(tmp_path)                                                         # (50.25-49.75)/50 = exactly 1.00%
    assert enter(h, Quotes(AAA=(49.75, 50.25), age=0.2), entry="51.00", stop="50.80").state == "entry_open"


def test_a_spread_skip_is_never_retried_that_day(tmp_path):
    h = harness(tmp_path)
    enter(h, Quotes(KOD=(96.62, 111.67)), symbol="KOD", entry="97.88", stop="97.19")
    h.engine.quote_source = Quotes(KOD=(97.40, 97.50))                            # the spread narrows later
    h.engine.submit_entry(h.trade("KOD"))
    h.engine.tick(h.clock.now())
    h.engine.recover(h.clock.now())
    assert h.sim.submitted == [] and h.trade("KOD").state == "not_submitted"
