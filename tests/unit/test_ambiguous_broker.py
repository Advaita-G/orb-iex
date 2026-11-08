"""HTTP 5xx / timeouts / connection resets are AMBIGUOUS, never a rejection (P0-A item 2): "the broker accepted the
order but the HTTP reply was lost". Look the order up before doing anything else; never create a second entry."""

import itertools
from decimal import Decimal as D
from urllib.parse import urlsplit

import pytest
import requests

from fakes import FakeResponse
from helpers import Harness
from orb_iex.broker import (AMBIGUOUS_ATTEMPTS, AlpacaPaperBroker, BrokerAmbiguous, OrderRejected, OrderUncertain,
                            is_ambiguous_status)
from orb_iex.http import ReadOnlyClient
from orb_iex.orders import OrderRequest
from orb_iex.secrets import Credentials

CREDS = Credentials("PKFAKEKEYID9876", "fake-secret-value-abc")
AMBIGUOUS = ["500", "502", "503", "504", "reset", "timeout"]
GET_ATTEMPTS = 4          # the read-only client retries a GET 5xx this many times before giving up


class FakeAlpaca:
    """A stateful Alpaca. Writes can be ACCEPTED while their reply is lost (5xx or connection reset), or the
    request can fail BEFORE it is processed (`drop`). Lookups can fail too (`lookup_fail`)."""

    def __init__(self, lose=0, how="504", drop=0, lookup_fail=0):
        self.orders: dict[str, dict] = {}
        self.ids = itertools.count(1)
        self.calls: list[tuple] = []
        self.lose, self.how, self.drop, self.lookup_fail = lose, how, drop, lookup_fail
        self.positions: list[dict] = []

    # -- helpers
    def posts(self, cid=None):
        return [c for c in self.calls if c[0] == "POST" and (cid is None or c[2]["client_order_id"] == cid)]

    def by_cid(self, cid):
        return next((o for o in self.orders.values() if o["client_order_id"] == cid), None)

    def _lose(self, resp):
        if self.lose > 0:                                  # processed, but the answer never arrives
            self.lose -= 1
            return self._fail()
        return resp

    def _fail(self):
        if self.how in ("reset", "timeout"):
            raise (requests.ConnectionError("Connection reset by peer") if self.how == "reset"
                   else requests.ReadTimeout("read timed out"))
        return FakeResponse(int(self.how), {"message": "upstream error"}, text="upstream error")

    def _new(self, body, **extra):
        oid = f"o-{next(self.ids)}"
        o = {"id": oid, "client_order_id": body["client_order_id"], "symbol": body.get("symbol", "AAA"),
             "side": body.get("side", "buy"), "type": body.get("type", "stop"), "qty": str(body.get("qty", 10)),
             "status": "new", "filled_qty": "0", "stop_price": body.get("stop_price")}
        o.update(extra)
        self.orders[oid] = o
        return o

    # -- the transport interface
    def request(self, method, url, params=None, headers=None, timeout=None, allow_redirects=True, json=None):
        path = urlsplit(url).path
        self.calls.append((method, path, json if method != "GET" else (params or {})))
        if method == "GET":
            return self._get(path, params or {})
        if self.drop > 0:                                  # fails before Alpaca processed it
            self.drop -= 1
            return self._fail()
        if method == "POST":
            if self.by_cid(json["client_order_id"]):
                return self._lose(FakeResponse(422, {"message": "client_order_id must be unique"}))
            return self._lose(FakeResponse(200, self._new(json)))
        oid = path.rsplit("/", 1)[1]
        if method == "DELETE":
            o = self.orders[oid]
            if o["status"] in ("canceled", "filled", "replaced"):
                return self._lose(FakeResponse(422, {"message": "order is not cancelable"}))
            o["status"] = "canceled"
            return self._lose(FakeResponse(204, None))
        if method == "PATCH":
            old = self.orders[oid]
            if old["status"] != "new":
                return self._lose(FakeResponse(422, {"message": f"cannot replace order in {old['status']}"}))
            old["status"] = "replaced"
            new = self._new({**old, **json}, replaces=oid, stop_price=json.get("stop_price", old["stop_price"]))
            old["replaced_by"] = new["id"]
            return self._lose(FakeResponse(200, new))
        raise AssertionError(method)

    def _get(self, path, params):
        if path in ("/v2/orders:by_client_order_id", ) or path.startswith("/v2/orders/"):
            if self.lookup_fail > 0:
                self.lookup_fail -= 1
                return FakeResponse(503, {"message": "unavailable"}, text="unavailable")
            o = (self.by_cid(params["client_order_id"]) if path == "/v2/orders:by_client_order_id"
                 else self.orders.get(path.rsplit("/", 1)[1]))
            return FakeResponse(200, o) if o else FakeResponse(404, {"message": "order not found"})
        if path == "/v2/positions":
            return FakeResponse(200, self.positions)
        if path == "/v2/orders":
            live = ("new", "accepted")
            return FakeResponse(200, [o for o in self.orders.values()
                                      if params.get("status") == "all" or o["status"] in live])
        return FakeResponse(404, {"message": "not found"})


def make(alpaca, tmp_path=None):
    c = ReadOnlyClient(CREDS, 150, 150, transport=alpaca, clock=lambda: 0.0, sleep=lambda s: None)
    b = AlpacaPaperBroker(c, str((tmp_path or __import__("pathlib").Path("/nonexistent")) / "KILL"),
                          sleep=lambda s: None)
    b.arm(mode="paper", enable_paper_orders=True, account_verified=True)
    return b


def req(role="entry", qty=10):
    side, typ = ("buy", "stop") if role in ("entry",) else (("sell", "stop") if role.startswith("stop") else
                                                            ("sell", "market"))
    return OrderRequest("AAA", side, D(qty), typ, f"orbiex-20260924-AAA-{role}", D(50) if typ == "stop" else None)


def test_status_classification():
    for s in (500, 502, 503, 504, 599, 408):
        assert is_ambiguous_status(s)
    for s in (200, 201, 204, 400, 401, 403, 404, 422, 429):
        assert not is_ambiguous_status(s)
    assert not is_ambiguous_status(None)


# --- ENTRY: accepted at Alpaca, reply lost -> adopted; never a second entry -------------------------------------------

@pytest.mark.parametrize("how", AMBIGUOUS)
def test_entry_accepted_but_reply_lost_is_adopted_and_never_resubmitted(how):
    a = FakeAlpaca(lose=1, how=how)
    u = make(a).submit_order(req("entry"))
    assert u.client_order_id == "orbiex-20260924-AAA-entry" and u.status == "new"
    assert len(a.posts()) == 1 and len(a.orders) == 1                  # exactly one POST, exactly one order


@pytest.mark.parametrize("how", AMBIGUOUS)
def test_entry_not_accepted_and_reply_lost_fails_closed_without_resend(how):
    a = FakeAlpaca(drop=1, how=how)                                     # failed before Alpaca processed it
    with pytest.raises(OrderUncertain) as exc:
        make(a).submit_order(req("entry"))
    assert type(exc.value) is OrderUncertain                           # unconfirmed, NOT a definitive rejection
    assert len(a.posts()) == 1 and a.orders == {}                       # never an automatic second attempt


def test_entry_5xx_and_the_lookup_also_fails_is_still_unconfirmed_and_not_resent():
    a = FakeAlpaca(lose=1, how="503", lookup_fail=10)
    with pytest.raises(OrderUncertain):
        make(a).submit_order(req("entry"))
    assert len(a.posts()) == 1                                          # the order may exist: no second entry
    assert len(a.orders) == 1                                           # (and it does: a later reconcile adopts it)


def test_4xx_is_still_a_definitive_rejection():
    class Rejecting(FakeAlpaca):
        def request(self, method, url, params=None, headers=None, timeout=None, allow_redirects=True, json=None):
            if method == "POST":
                self.calls.append((method, urlsplit(url).path, json))
                return FakeResponse(403, {"message": "insufficient buying power"})
            return super().request(method, url, params, headers, timeout, allow_redirects, json)
    with pytest.raises(OrderRejected) as exc:
        make(Rejecting()).submit_order(req("entry"))
    assert type(exc.value) is OrderRejected and exc.value.status == 403


# --- STOP / EXIT: converge to protection or flat ---------------------------------------------------------------------

@pytest.mark.parametrize("role", ["stop", "eod", "flatten", "stopfail"])
def test_stop_or_exit_accepted_but_reply_lost_is_adopted_without_a_resend(role):
    a = FakeAlpaca(lose=1, how="502")
    u = make(a).submit_order(req(role))
    assert u.client_order_id.endswith(role) and len(a.posts()) == 1 and len(a.orders) == 1


@pytest.mark.parametrize("role", ["stop", "eod"])
def test_stop_or_exit_not_processed_is_resent_with_the_same_id_until_it_exists(role):
    a = FakeAlpaca(drop=2, how="503")                                   # two failures before it is processed
    u = make(a).submit_order(req(role))
    assert u.status == "new" and len(a.orders) == 1
    assert len(a.posts()) == 3 and {c[2]["client_order_id"] for c in a.posts()} == {u.client_order_id}


def test_stop_resend_after_a_lost_reply_and_failed_lookup_hits_the_duplicate_guard_not_a_second_order():
    a = FakeAlpaca(lose=1, how="504", lookup_fail=GET_ATTEMPTS)          # accepted; the first lookup fails too
    b = make(a)
    with pytest.raises(OrderUncertain):
        b.submit_order(req("stop"))
    assert len(a.posts()) == 1 and len(a.orders) == 1
    assert b.get_order_by_client_id(req("stop").client_order_id).order_id == "o-1"


def test_stop_never_confirmed_gives_up_unconfirmed_after_the_attempts():
    a = FakeAlpaca(drop=99, how="500")
    with pytest.raises(OrderUncertain):
        make(a).submit_order(req("stop"))
    assert len(a.posts()) == AMBIGUOUS_ATTEMPTS and a.orders == {}      # the engine then retries / flattens


# --- CANCEL / REPLACE -------------------------------------------------------------------------------------------------

def test_cancel_applied_but_reply_lost_is_confirmed_from_the_order_state():
    a = FakeAlpaca()
    b = make(a)
    oid = b.submit_order(req("entry")).order_id
    a.lose, a.how = 1, "504"
    b.cancel_order(oid)                                                  # no exception: confirmed canceled
    assert a.orders[oid]["status"] == "canceled" and sum(c[0] == "DELETE" for c in a.calls) == 1


def test_cancel_not_applied_is_resent_while_the_order_is_confirmed_live():
    a = FakeAlpaca()
    b = make(a)
    oid = b.submit_order(req("entry")).order_id
    a.drop, a.how = 1, "503"
    b.cancel_order(oid)
    assert a.orders[oid]["status"] == "canceled" and sum(c[0] == "DELETE" for c in a.calls) == 2


def test_cancel_that_never_confirms_is_unconfirmed_not_rejected():
    a = FakeAlpaca()
    b = make(a)
    oid = b.submit_order(req("entry")).order_id
    a.drop, a.how = 99, "500"
    with pytest.raises(OrderUncertain):
        b.cancel_order(oid)
    assert a.orders[oid]["status"] == "new"                              # the caller keeps working on it
    assert sum(c[0] == "DELETE" for c in a.calls) == AMBIGUOUS_ATTEMPTS


def test_cancel_lookup_failure_never_retries_blindly():
    a = FakeAlpaca()
    b = make(a)
    oid = b.submit_order(req("entry")).order_id
    a.lose, a.how, a.lookup_fail = 1, "504", GET_ATTEMPTS
    with pytest.raises(OrderUncertain):
        b.cancel_order(oid)
    assert sum(c[0] == "DELETE" for c in a.calls) == 1
    assert b.get_order(oid).status == "canceled"


def test_replace_applied_but_reply_lost_adopts_the_new_order():
    a = FakeAlpaca()
    b = make(a)
    old = b.submit_order(req("stop")).order_id
    a.lose, a.how = 1, "504"
    u = b.replace_order(old, D(5), "orbiex-20260924-AAA-stop2", stop_price=D("49.5"))
    assert u.client_order_id == "orbiex-20260924-AAA-stop2" and u.replaces == old
    assert sum(c[0] == "PATCH" for c in a.calls) == 1 and a.orders[old]["status"] == "replaced"


def test_replace_not_applied_is_resent_only_because_the_old_order_is_confirmed_unchanged():
    a = FakeAlpaca()
    b = make(a)
    old = b.submit_order(req("stop")).order_id
    a.drop, a.how = 1, "503"
    u = b.replace_order(old, D(5), "orbiex-20260924-AAA-stop2")
    assert sum(c[0] == "PATCH" for c in a.calls) == 2 and u.replaces == old


def test_replace_with_an_unconfirmed_old_order_is_never_resent():
    a = FakeAlpaca()
    b = make(a)
    old = b.submit_order(req("stop")).order_id
    a.lose, a.how, a.lookup_fail = 1, "504", 99                          # applied, and nothing can be looked up
    with pytest.raises(OrderUncertain):
        b.replace_order(old, D(5), "orbiex-20260924-AAA-stop2")
    assert sum(c[0] == "PATCH" for c in a.calls) == 1                    # no blind second PATCH


def test_lookup_distinguishes_absent_from_unknown():
    a = FakeAlpaca()
    b = make(a)
    assert b.get_order_by_client_id("orbiex-20260924-AAA-entry") is None   # definite 404
    a.lookup_fail = GET_ATTEMPTS
    with pytest.raises(BrokerAmbiguous):
        b.get_order_by_client_id("orbiex-20260924-AAA-entry")              # 503 is "don't know", not "absent"
    assert issubclass(BrokerAmbiguous, ConnectionError)


# --- through the engine ----------------------------------------------------------------------------------------------

def place(h, symbol="AAA"):
    h.open_session()
    h.j.save_candidates(h.day, [{"symbol": symbol, "rank": 1, "status": "candidate"}])
    h.engine.place_entries([h.plan(symbol)], {symbol: {"tradable": True, "shortable": True, "easy_to_borrow": True}},
                           {"buying_power": "1000000", "shorting_enabled": True}, h.clock.now())
    return h.trade(symbol)


@pytest.mark.parametrize("how", ["500", "503", "504", "reset"])
def test_engine_entry_accepted_but_reply_lost_becomes_the_live_entry_not_a_rejection(tmp_path, how):
    a = FakeAlpaca(lose=1, how=how)
    h = Harness(tmp_path, sim=make(a, tmp_path))
    t = place(h)
    assert len(a.posts()) == 1 and len(a.orders) == 1                    # one entry at Alpaca, ever
    assert t.state == "entry_open" and t.entry_order_id == "o-1"
    cand = [c for c in h.j.candidates(h.day) if c["symbol"] == "AAA"][0]
    assert cand["status"] == "submitted" and cand["reason"] != "entry_rejected"
    h.engine.reconcile(h.clock.now())                                    # the next poll changes nothing
    assert len(a.posts()) == 1


def test_engine_entry_unconfirmed_fails_closed_and_is_not_marked_rejected(tmp_path):
    a = FakeAlpaca(drop=1, how="503")
    h = Harness(tmp_path, sim=make(a, tmp_path))
    t = place(h)
    assert len(a.posts()) == 1 and a.orders == {}
    assert t.state == "entry_open" and t.entry_order_id is None and t.state != "entry_rejected"
    cand = [c for c in h.j.candidates(h.day) if c["symbol"] == "AAA"][0]
    assert cand["status"] == "submitted" and cand["reason"] == "unconfirmed"
    assert h.engine.stats["entry_unconfirmed"] == 1
    h.engine.tick(h.clock.now())
    h.engine.reconcile(h.clock.now())
    assert len(a.posts()) == 1                                           # still never a blind second entry


def test_engine_stop_accepted_but_reply_lost_is_the_one_protective_stop(tmp_path):
    a = FakeAlpaca()
    h = Harness(tmp_path, sim=make(a, tmp_path))
    t = place(h)
    a.orders["o-1"].update(status="filled", filled_qty="100")
    h.sim.latest_quote = lambda symbol: {'t': h.clock.now().isoformat(), 'bp': '50', 'ap': '50'}
    a.positions = [{"symbol": "AAA", "qty": "100", "side": "long"}]
    a.lose, a.how = 1, "502"                                             # the stop is accepted, its reply is lost
    h.engine.reconcile(h.clock.now())
    stops = [o for o in a.orders.values() if o["client_order_id"].endswith("-stop1")]
    assert len(stops) == 1 and len(a.posts("orbiex-20260924-AAA-stop1")) == 1
    tr = h.trade("AAA")
    assert tr.state == "active" and tr.stop_order_id == stops[0]["id"] and not tr.closed
    assert t.symbol == "AAA"
