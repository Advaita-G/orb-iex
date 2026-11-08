"""Quote age (P0-A item 5): an entry decision never uses a stale, undated or future-dated quote. The skip reason is
JOURNALED (candidate row, trade row, trading-journal event), not only logged, together with the exchange timestamp,
receipt timestamp, age, bid, ask, spread and feed."""

import datetime as dt
import logging
from dataclasses import replace
from decimal import Decimal as D

import pytest

from helpers import Harness
from orb_iex.config import ConfigError, parse_settings
from orb_iex.marketdata import Quote, parse_quote
from orb_iex.tradelog import TradeLog


class Quotes:
    """MarketData.entry_quotes stand-in: bid/ask per symbol, stamped `age` seconds before the request time."""

    def __init__(self, age=0.0, stamp=True, **q):
        self.age, self.stamp = age, stamp
        self.q = {s: Quote(s, D(str(b)), D(str(a))) for s, (b, a) in q.items()}
        self.calls = []

    def __call__(self, symbols, now):
        self.calls.append(list(symbols))
        t = now - dt.timedelta(seconds=self.age) if self.stamp else None
        return {s: replace(self.q[s], t=t) for s in symbols if s in self.q}


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def logs():
    lg = logging.getLogger("orb_iex")
    h = Capture()
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.DEBUG)
    yield h
    lg.removeHandler(h)
    lg.setLevel(old)


def harness(tmp_path, **over):
    h = Harness(tmp_path, **over)
    h.open_session()
    h.engine.tlog = TradeLog(tmp_path / "tl.sqlite", tmp_path / "exports")
    return h


def entry(h, quotes, symbol="AAA", direction=1, **kw):
    h.engine.quote_source = quotes
    h.j.save_candidates(h.day, [{"symbol": symbol, "rank": 1, "status": "candidate"}])
    h.engine.place_entries([h.plan(symbol, direction, 100, **kw)],
                           {symbol: {"tradable": True, "shortable": True, "easy_to_borrow": True}},
                           {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    return h.trade(symbol)


def cand(h, sym="AAA"):
    return [c for c in h.j.candidates(h.day) if c["symbol"] == sym][0]


def events(h, kind):
    return [e for e in h.engine.tlog.events(h.day.isoformat()) if e["kind"] == kind]


def sent(h, sym="AAA"):
    return [r for r in h.sim.submitted if r.symbol == sym and r.client_order_id.endswith("-entry")]


# --- config -----------------------------------------------------------------------------------------------------------

def test_max_quote_age_is_a_validated_setting():
    assert parse_settings({}).max_quote_age_seconds == 2            # default 2 s (it was 15 s in the first P0-A commit)
    assert parse_settings({"trading": {"max_quote_age_seconds": 3}}).max_quote_age_seconds == 3
    assert "maxQuoteAge=3s" in parse_settings({"trading": {"max_quote_age_seconds": 3}}).config_line()
    for bad in (0, -1, 301, "15", True):
        with pytest.raises(ConfigError):
            parse_settings({"trading": {"max_quote_age_seconds": bad}})


def test_parse_quote_keeps_the_exchange_timestamp_and_feed():
    q = parse_quote("AAA", {"bp": 49.8, "ap": 49.9, "t": "2026-09-24T13:35:02.123456789Z"})
    assert q.t == dt.datetime(2026, 9, 24, 13, 35, 2, 123456, tzinfo=dt.timezone.utc) and q.feed == "iex"
    assert parse_quote("AAA", {"bp": 49.8, "ap": 49.9}).t is None


# --- fresh quotes are used -----------------------------------------------------------------------------------------------

def test_a_fresh_quote_is_used_and_fully_journaled(tmp_path, logs):
    h = harness(tmp_path)
    t = entry(h, Quotes(age=2, AAA=(49.80, 49.90)))
    assert t.state == "entry_open" and len(sent(h)) == 1
    ev = events(h, "entry_quote")
    assert len(ev) == 1 and ev[0]["status"] == "usable" and ev[0]["symbol"] == "AAA"
    d = ev[0]["detail"]
    d = d if isinstance(d, dict) else __import__("json").loads(d)
    assert d["quote_age_seconds"] == 2.0 and d["bid"] == "49.8" and d["ask"] == "49.9" and d["spread"] == "0.1"
    assert d["feed"] == "iex" and d["quote_exchange_time"] and d["received_at"] and d["max_quote_age_seconds"] == 2
    rec = [r for r in logs.records if getattr(r, "event", "") == "entry_quote"][0]
    f = rec.fields
    for k in ("quote_exchange_time", "received_at", "quote_age_seconds", "bid", "ask", "spread", "feed"):
        assert f[k] is not None, k
    assert f["verdict"] == "usable" and "age=2.0s" in rec.getMessage() and "feed=iex" in rec.getMessage()


def test_age_exactly_at_the_limit_is_still_usable(tmp_path):
    h = harness(tmp_path)
    assert entry(h, Quotes(age=2, AAA=(49.80, 49.90))).state == "entry_open"            # the 2 s default
    h2 = harness(tmp_path / "b")
    assert entry(h2, Quotes(age=2.01, AAA=(49.80, 49.90))).state == "not_submitted"     # just over: skipped
    h3 = harness(tmp_path / "c", trading__max_quote_age_seconds=15)                     # and it stays configurable
    assert entry(h3, Quotes(age=15, AAA=(49.80, 49.90))).state == "entry_open"


# --- stale / undated / future quotes are not used: skipped and journaled -------------------------------------------------

def stale_checks(h, t, reason):
    assert t.state == "not_submitted" and t.note == reason and t.entry_order_id is None
    assert sent(h) == [] and h.sim.submitted == []                  # no order of any kind, no IOC chasing, no market
    c = cand(h)
    assert c["status"] == "skipped" and c["reason"] == reason       # the candidate journal says why
    skip = events(h, "skip")
    assert len(skip) == 1 and skip[0]["status"] == "skipped"        # and so does the trading journal
    d = skip[0]["detail"]
    d = d if isinstance(d, dict) else __import__("json").loads(d)
    assert d["reason"] == reason
    for k in ("quote_exchange_time", "received_at", "quote_age_seconds", "bid", "ask", "spread", "feed",
              "max_quote_age_seconds"):
        assert k in d, k
    assert events(h, "entry_quote")[0]["status"] == reason


def test_a_quote_older_than_the_limit_is_skipped_with_a_journaled_reason(tmp_path, logs):
    h = harness(tmp_path)
    t = entry(h, Quotes(age=16, AAA=(49.80, 49.90)))
    stale_checks(h, t, "stale_quote")
    assert h.engine.stats["stale_quote_skipped"] == 1
    warn = [r for r in logs.records if getattr(r, "event", "") == "entry_skip"][0]
    assert warn.fields["reason"] == "stale_quote" and warn.fields["quote_age_seconds"] == 16.0
    assert "stale_quote" in warn.getMessage()


def test_the_limit_is_configurable(tmp_path):
    h = harness(tmp_path, trading__max_quote_age_seconds=5)
    stale_checks(h, entry(h, Quotes(age=6, AAA=(49.80, 49.90))), "stale_quote")
    h2 = harness(tmp_path / "b", trading__max_quote_age_seconds=5)
    assert entry(h2, Quotes(age=4, AAA=(49.80, 49.90))).state == "entry_open"
    h3 = harness(tmp_path / "c", trading__max_quote_age_seconds=60)
    assert entry(h3, Quotes(age=45, AAA=(49.80, 49.90))).state == "entry_open"


def test_an_undated_quote_cannot_be_aged_so_it_is_not_used(tmp_path):
    h = harness(tmp_path)
    t = entry(h, Quotes(stamp=False, AAA=(49.80, 49.90)))
    stale_checks(h, t, "quote_no_timestamp")


def test_a_quote_from_the_future_is_bad_data(tmp_path):
    h = harness(tmp_path)
    stale_checks(h, entry(h, Quotes(age=-10, AAA=(49.80, 49.90))), "quote_from_future")
    h2 = harness(tmp_path / "b")
    assert entry(h2, Quotes(age=-1, AAA=(49.80, 49.90))).state == "entry_open"      # small clock skew is tolerated


def test_a_stale_quote_that_would_have_looked_crossed_is_skipped_as_stale_not_as_crossed(tmp_path):
    h = harness(tmp_path)
    t = entry(h, Quotes(age=60, AAA=(50.05, 50.10)))              # ask above the 50.00 trigger, but a minute old
    stale_checks(h, t, "stale_quote")
    assert h.engine.stats["entry_crossed_skipped"] == 0


def test_a_stale_quote_with_a_wide_spread_is_still_not_used(tmp_path):
    h = harness(tmp_path)
    stale_checks(h, entry(h, Quotes(age=60, AAA=(40.00, 60.00))), "stale_quote")


def test_a_skipped_stale_entry_is_never_retried_or_resubmitted(tmp_path):
    h = harness(tmp_path)
    t = entry(h, Quotes(age=60, AAA=(49.80, 49.90)))
    assert t.state == "not_submitted"
    h.engine.place_entries([h.plan("AAA", 1, 100)], {"AAA": {"tradable": True, "shortable": True}},
                           {"buying_power": "1000000"}, h.clock.now())
    h.engine.tick(h.clock.now())
    h.engine.reconcile(h.clock.now())
    h.engine.recover(h.clock.now())                                # even a restart
    assert h.sim.submitted == [] and h.trade("AAA").state == "not_submitted"


def test_a_stale_skip_frees_its_buying_power_for_the_next_entry(tmp_path):
    h = harness(tmp_path)
    q = Quotes(age=16, AAA=(49.80, 49.90))
    q.q["BBB"] = Quote("BBB", D("49.80"), D("49.90"))

    def source(symbols, now):                                     # AAA stale, BBB fresh
        out = Quotes.__call__(q, symbols, now)
        out["BBB"] = replace(q.q["BBB"], t=now)
        return out
    h.engine.quote_source = source
    h.j.save_candidates(h.day, [{"symbol": "AAA", "rank": 1, "status": "candidate"},
                                {"symbol": "BBB", "rank": 2, "status": "candidate"}])
    assets = {s: {"tradable": True, "shortable": True} for s in ("AAA", "BBB")}
    h.engine.place_entries([h.plan("AAA", 1, 100), h.plan("BBB", 1, 100)], assets,
                           {"buying_power": "5000", "shorting_enabled": True}, h.clock.now())    # room for ONE entry
    assert h.trade("AAA").state == "not_submitted" and h.trade("BBB").state == "entry_open"


def test_age_is_measured_at_decision_time_not_at_fetch_time(tmp_path):
    """The batch is fetched once; a later symbol is decided a moment later, so its quote is older then."""
    h = harness(tmp_path, trading__max_quote_age_seconds=15)
    q = Quotes(age=14, AAA=(49.80, 49.90), BBB=(49.80, 49.90))
    h.engine.quote_source = q
    real = h.sim.submit_order

    def slow_submit(req):                                          # each submit takes 2 s
        out = real(req)
        h.clock.sleep(2)
        return out
    h.sim.submit_order = slow_submit
    h.j.save_candidates(h.day, [{"symbol": "AAA", "rank": 1, "status": "candidate"},
                                {"symbol": "BBB", "rank": 2, "status": "candidate"}])
    assets = {s: {"tradable": True, "shortable": True} for s in ("AAA", "BBB")}
    h.engine.place_entries([h.plan("AAA", 1, 100), h.plan("BBB", 1, 100)], assets,
                           {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    assert len(q.calls) == 1                                       # still one request for the batch
    assert h.trade("AAA").state == "entry_open"                    # 14 s old when decided
    assert h.trade("BBB").state == "not_submitted" and cand(h, "BBB")["reason"] == "stale_quote"   # 16 s by then


# --- missing quote: FAIL CLOSED (see also tests/unit/test_quote_gate.py) -------------------------------------------------------

def test_no_quote_at_all_skips_the_entry(tmp_path):
    h = harness(tmp_path)
    t = entry(h, Quotes(), symbol="AAA")                                    # no AAA quote returned
    assert t.state == "not_submitted" and t.note == "skip_quote_unavailable" and h.sim.submitted == []
