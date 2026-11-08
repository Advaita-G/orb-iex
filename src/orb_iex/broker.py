"""The single order-write choke point: AlpacaPaperBroker.

This is the only module that sends POST, PATCH or DELETE to Alpaca.
Every write:
  1. rebuilds the URL from the PAPER constant and re-checks it (assert_paper_trading_url);
  2. refuses unless the broker was armed after all startup safety checks
     (paper mode + trading.enable_paper_orders + verified paper account);
  3. refuses a new ENTRY while the kill-switch file exists (exits/stops still allowed);
  4. never follows redirects.
A submit is idempotent: the deterministic client_order_id is resent on retry,
and a "client_order_id must be unique" reply is resolved by fetching that order.

Ambiguous replies (HTTP 5xx, timeouts, connection reset) are NEVER a rejection: Alpaca may have accepted
the request and lost the answer. The broker looks the order up (by client_order_id, or by order id for
cancel/replace) before it does anything else:
  - ENTRY submit: lookup only. Found = adopted. Not found = OrderUncertain (fail closed: no automatic
    resend; the entry-window check and the reconcile decide later). Never a second entry.
  - STOP / EXIT submit: lookup, then a resend with the SAME client_order_id (Alpaca rejects a duplicate id,
    so a resend cannot create a second order) until the order is confirmed or the attempts run out
    (OrderUncertain; the engine then converges to protection or flat).
  - REPLACE: adopted if the new id exists; resent only when the old order is confirmed still unchanged.
  - CANCEL: done if the order is confirmed terminal / pending_cancel; resent only while it is confirmed live.
"""

from __future__ import annotations

import logging
import re
import time
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import requests

from . import constants as C
from .http import ReadOnlyClient
from .logging_setup import LOGGER_NAME, log_event
from .orders import OrderRequest, OrderUpdate, TERMINAL, from_alpaca_order, parse_client_order_id, role_kind
from .safety import SafetyError, assert_paper_trading_url, kill_switch_active

log = logging.getLogger(LOGGER_NAME)


class OrderRejected(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body
        lower = message.lower()
        self.classification = next((kind for kind, words in (
            ('wash_trade', ('wash trade',)), ('held_quantity', ('held', 'available')),
            ('crossed_stop', ('current price', 'base price')), ('invalid_state', ('status', 'state')),
            ('price_increment', ('sub-penny', 'increment')),
            ('ambiguous', ('no reply', 'timeout', 'http 5')))
            if any(word in lower for word in words)), 'unknown')


class OrderUncertain(OrderRejected):
    """The request may or may not have reached Alpaca. A later poll adopts it if it did."""


class BrokerAmbiguous(ConnectionError):
    """A read answered neither 'here it is' nor 'it does not exist' (HTTP 5xx, bad body, network).
    Callers must not treat it as 'not found'."""


# HTTP statuses that say nothing about whether Alpaca acted on the request.
def is_ambiguous_status(status: int | None) -> bool:
    return status is not None and (status >= 500 or status == 408)


REPLACE_RESEND_STATUSES = frozenset({"new", "accepted"})
AMBIGUOUS_ATTEMPTS = 3          # lookup + same-id resend rounds for a stop/exit submit, cancel or replace
AMBIGUOUS_BACKOFF_SECONDS = (1.0, 2.0)


_CROSSED_HINTS = ("base price", "base_price", "current price", "market price", "last price",
                  "less than", "greater than", "lower than", "higher than", ">=", "<=", "must be")
_NOT_REPLACEABLE_RE = re.compile(r"cannot replace order|pending[_ ](new|cancel|replace)|not replaceable",
                                 re.IGNORECASE)


def is_crossed_stop_rejection(exc: Exception) -> bool:
    """Alpaca 422 saying a stop price is on the wrong side of the market (already crossed).
    Used for the ORB entry (skip) and for the protective stop (emergency flatten)."""
    if getattr(exc, "status", None) != 422:
        return False
    msg = str(exc).lower()
    return "stop" in msg and "price" in msg and any(h in msg for h in _CROSSED_HINTS)


def is_not_replaceable_rejection(exc: Exception) -> bool:
    """Alpaca refused a PATCH because the order is in a state that cannot be replaced
    (for example 'cannot replace order in pending_new'). Transient: retry later."""
    return getattr(exc, "status", None) in (422, 403) and bool(_NOT_REPLACEABLE_RE.search(str(exc)))


class Broker(Protocol):
    def submit_order(self, req: OrderRequest) -> OrderUpdate: ...
    def replace_order(self, order_id: str, qty: Decimal, client_order_id: str,
                      stop_price: Decimal | None = None) -> OrderUpdate: ...
    def cancel_order(self, order_id: str) -> None: ...
    def get_order_by_client_id(self, cid: str) -> OrderUpdate | None: ...
    def list_orders(self, status: str, after: str | None = None) -> list[OrderUpdate]: ...
    def list_positions(self) -> dict[str, Decimal]: ...
    def get_account(self) -> dict: ...


def positions_from_alpaca(rows: list[dict]) -> dict[str, Decimal]:
    out = {}
    for p in rows:
        qty = abs(Decimal(str(p.get("qty", "0"))))
        if qty:
            out[p["symbol"]] = -qty if p.get("side") == "short" else qty
    return out


class AlpacaPaperBroker:
    def __init__(self, client: ReadOnlyClient, kill_switch_file: str, sleep=time.sleep):
        self._client = client
        self._kill_switch_file = kill_switch_file
        self._armed = False
        self._sleep = sleep
        self.write_counts = {"POST": 0, "PATCH": 0, "DELETE": 0}

    # -- arming -------------------------------------------------------------
    def arm(self, *, mode: str, enable_paper_orders: bool, account_verified: bool) -> None:
        if mode != "paper":
            raise SafetyError(f"order writes need mode=paper, got {mode!r}")
        if not enable_paper_orders:
            raise SafetyError("trading.enable_paper_orders is false in settings; paper orders refused")
        if not account_verified:
            raise SafetyError("paper account was not verified; paper orders refused")
        assert_paper_trading_url(C.PAPER_TRADING_BASE_URL + "/v2/orders")
        self._armed = True
        log_event(log, logging.WARNING, "broker_armed",
                  f"paper order submission ENABLED on {C.PAPER_TRADING_HOST}")

    @property
    def armed(self) -> bool:
        return self._armed

    # -- the choke point -------------------------------------------------------
    def _write(self, method: str, path: str, body: dict | None, purpose: str):
        url = C.PAPER_TRADING_BASE_URL + path
        assert_paper_trading_url(url)                     # raises before any I/O
        if not self._armed:
            raise SafetyError(f"broker not armed; refusing {method} {path}")
        c = self._client
        host = urlsplit(url).hostname
        for attempt in (1, 2):
            before_write = getattr(self, 'before_write', None)
            if before_write is not None:
                before_write()
            c._limiters[host].acquire()
            guard = getattr(self, 'mutation_guard', None)
            if guard is not None:
                guard(method, body)  # after rate-limit waits, before every HTTP write/retry
            c.call_counts[host] += 1
            self.write_counts[method] += 1
            try:
                resp = c._transport.request(method, url, json=body, headers=c._credentials.headers(),
                                            timeout=c._timeout, allow_redirects=False)
            except requests.RequestException as exc:
                log_event(log, logging.WARNING, "http_error", f"{method} {path} failed: {type(exc).__name__}",
                          method=method, path=path, purpose=purpose)
                return None
            if 300 <= resp.status_code < 400:
                raise SafetyError(f"unexpected redirect on {method} {path}; refusing")
            log_event(log, logging.DEBUG, "http_call", f"{method} {path} -> {resp.status_code} ({purpose})",
                      method=method, path=path, status=resp.status_code, purpose=purpose)
            if resp.status_code >= 400:
                log_event(log, logging.ERROR, 'broker_error_body', f'{method} {path}: HTTP {resp.status_code}',
                          method=method, path=path, status=resp.status_code, body=self._json(resp) or resp.text)
            if resp.status_code == 429 and attempt == 1:
                self._sleep(2.0)
                continue
            return resp
        return resp

    @staticmethod
    def _json(resp) -> Any:
        try:
            return resp.json()
        except ValueError:
            return None

    @staticmethod
    def _message(resp) -> str:
        data = AlpacaPaperBroker._json(resp)
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
        return (getattr(resp, "text", "") or "")[:200]

    # -- orders -------------------------------------------------------------
    @staticmethod
    def _ambiguous(resp) -> bool:
        return resp is None or is_ambiguous_status(resp.status_code)

    def _pause(self, attempt: int) -> None:
        self._sleep(AMBIGUOUS_BACKOFF_SECONDS[min(attempt - 1, len(AMBIGUOUS_BACKOFF_SECONDS) - 1)])

    def _log_ambiguous(self, what: str, resp, attempt: int, **fields) -> None:
        status = "no reply" if resp is None else f"HTTP {resp.status_code}"
        log_event(log, logging.WARNING, "broker_ambiguous",
                  f"{what}: {status} is NOT a rejection; looking the order up before doing anything else "
                  f"(attempt {attempt}/{AMBIGUOUS_ATTEMPTS})", attempt=attempt,
                  status=None if resp is None else resp.status_code,
                  body=None if resp is None else self._json(resp) or resp.text, **fields)

    def submit_order(self, req: OrderRequest) -> OrderUpdate:
        parsed = parse_client_order_id(req.client_order_id)
        if parsed is None:
            raise SafetyError(f"client_order_id {req.client_order_id!r} is not an ORB-IEX id")
        kind = role_kind(parsed[2])
        if kind == "entry" and kill_switch_active(self._kill_switch_file):
            raise SafetyError(f"kill switch {self._kill_switch_file} is active; new entry refused")
        cid = req.client_order_id
        resp = self._write("POST", "/v2/orders", req.payload(), f"submit:{cid}")
        for attempt in range(1, AMBIGUOUS_ATTEMPTS + 1):
            if not self._ambiguous(resp):
                return self._finish_submit(req, resp)
            self._log_ambiguous(f"submit {cid}", resp, attempt, client_order_id=cid)
            try:
                found = self.get_order_by_client_id(cid)
            except BrokerAmbiguous as exc:
                raise OrderUncertain(f"submit {cid} lookup failed; reconcile before retry: {exc}",
                                     None if resp is None else resp.status_code,
                                     None if resp is None else self._json(resp) or resp.text) from exc
            if found is not None:
                log_event(log, logging.WARNING, "ambiguous_submit_adopted",
                          f"{cid} exists at Alpaca (status {found.status}); adopted, not resubmitted",
                          client_order_id=cid, status=found.status)
                return found
            if kind == "entry" or attempt == AMBIGUOUS_ATTEMPTS:
                # An entry is never resent automatically (fail closed); a stop/exit that is still unconfirmed
                # goes back to the engine, which converges to protection or flat.
                raise OrderUncertain(f"submit {cid} is ambiguous and the order was not found at Alpaca",
                                     None if resp is None else resp.status_code,
                                     None if resp is None else self._json(resp) or resp.text)
            self._pause(attempt)                     # same client_order_id: a duplicate cannot become a 2nd order
            resp = self._write("POST", "/v2/orders", req.payload(), f"submit:{cid}")
        raise OrderUncertain(f"submit {cid} is ambiguous")             # pragma: no cover

    def _finish_submit(self, req: OrderRequest, resp) -> OrderUpdate:
        if resp.status_code in (200, 201):
            return from_alpaca_order(self._json(resp), event="submitted", source="submit")
        msg = self._message(resp)
        if resp.status_code == 422 and "client_order_id" in msg.lower():
            try:
                existing = self.get_order_by_client_id(req.client_order_id)
            except BrokerAmbiguous as exc:
                raise OrderUncertain(f"{req.client_order_id} is a duplicate id but the lookup failed: {exc}") from exc
            if existing:
                log_event(log, logging.WARNING, "duplicate_submit",
                          f"{req.client_order_id} already exists at Alpaca; adopted, not resubmitted")
                return existing
        raise OrderRejected(f"HTTP {resp.status_code}: {msg}", resp.status_code, self._json(resp) or resp.text)

    def replace_order(self, order_id: str, qty: Decimal, client_order_id: str,
                      stop_price: Decimal | None = None) -> OrderUpdate:
        body = {"qty": str(qty), "client_order_id": client_order_id}
        if stop_price is not None:
            body["stop_price"] = str(stop_price)
        path = f"/v2/orders/{quote(order_id)}"
        resp = self._write("PATCH", path, body, f"replace:{client_order_id}")
        for attempt in range(1, AMBIGUOUS_ATTEMPTS + 1):
            if not self._ambiguous(resp):
                if resp.status_code in (200, 201):
                    return from_alpaca_order(self._json(resp), event="replaced_new", source="submit")
                raise OrderRejected(f"HTTP {resp.status_code}: {self._message(resp)}", resp.status_code, self._json(resp) or resp.text)
            self._log_ambiguous(f"replace {order_id}", resp, attempt, order_id=order_id)
            try:
                new = self.get_order_by_client_id(client_order_id)
            except BrokerAmbiguous:
                new = None
            if new is not None:
                log_event(log, logging.WARNING, "ambiguous_replace_adopted",
                          f"replace {order_id} did happen: {client_order_id} exists; adopted",
                          client_order_id=client_order_id)
                return new
            try:
                old = self.get_order(order_id)
            except BrokerAmbiguous:
                old = None
            # resend only when the old order is CONFIRMED live and unchanged: then the PATCH did not apply
            if old is None or old.status not in REPLACE_RESEND_STATUSES or attempt == AMBIGUOUS_ATTEMPTS:
                raise OrderUncertain(f"replace {order_id} is ambiguous; the outcome is not confirmed")
            self._pause(attempt)
            resp = self._write("PATCH", path, body, f"replace:{client_order_id}")
        raise OrderUncertain(f"replace {order_id} is ambiguous")       # pragma: no cover

    def cancel_order(self, order_id: str) -> None:
        path = f"/v2/orders/{quote(order_id)}"
        resp = self._write("DELETE", path, None, f"cancel:{order_id}")
        for attempt in range(1, AMBIGUOUS_ATTEMPTS + 1):
            if not self._ambiguous(resp):
                if resp.status_code in (200, 204):
                    return
                raise OrderRejected(f"HTTP {resp.status_code}: {self._message(resp)}", resp.status_code, self._json(resp) or resp.text)
            self._log_ambiguous(f"cancel {order_id}", resp, attempt, order_id=order_id)
            try:
                old = self.get_order(order_id)
            except BrokerAmbiguous:
                old = None
            if old is not None and (old.status in TERMINAL or old.status == "pending_cancel"):
                return                                       # confirmed: nothing left to cancel
            if old is None or attempt == AMBIGUOUS_ATTEMPTS:
                raise OrderUncertain(f"cancel {order_id} is ambiguous; not confirmed canceled")
            self._pause(attempt)                             # a repeated cancel is harmless
            resp = self._write("DELETE", path, None, f"cancel:{order_id}")
        raise OrderUncertain(f"cancel {order_id} is ambiguous")        # pragma: no cover

    # -- reads (GET, through the read-only client) -------------------------------
    def _order_lookup(self, path: str, params: dict | None, purpose: str, what: str) -> OrderUpdate | None:
        """OrderUpdate if Alpaca has the order, None ONLY on a definite 404, else BrokerAmbiguous."""
        try:
            resp = self._client.trading_get(path, params=params, purpose=purpose)
        except ConnectionError as exc:
            raise BrokerAmbiguous(f"lookup {what} failed: {exc}") from exc
        if resp.ok and isinstance(resp.data, dict) and resp.data.get("id"):
            return from_alpaca_order(resp.data, source="rest")
        if resp.status == 404:
            return None
        raise BrokerAmbiguous(f"lookup {what} was inconclusive: HTTP {resp.status}")

    def get_order_by_client_id(self, cid: str) -> OrderUpdate | None:
        return self._order_lookup("/v2/orders:by_client_order_id", {"client_order_id": cid}, "order_by_cid", cid)

    def get_order(self, order_id: str) -> OrderUpdate | None:
        return self._order_lookup(f"/v2/orders/{quote(order_id)}", None, "order_by_id", order_id)

    def list_orders(self, status: str, after: str | None = None) -> list[OrderUpdate]:
        params = {"status": status, "limit": 500, "direction": "asc", "nested": "false"}
        if after:
            params["after"] = after
        resp = self._client.trading_get("/v2/orders", params=params, purpose=f"orders:{status}")
        if not resp.ok or not isinstance(resp.data, list):
            raise ConnectionError(f"/v2/orders failed: HTTP {resp.status}")
        if len(resp.data) >= 500:
            raise ConnectionError('order snapshot may be truncated; reconciliation refused')
        return [from_alpaca_order(o, source="rest") for o in resp.data]

    def latest_quote(self, symbol: str) -> dict:
        resp = self._client.data_get(f'/v2/stocks/{quote(symbol, safe="")}/quotes/latest',
                                     params={'feed': 'iex'}, purpose='protection_quote')
        if not resp.ok or not isinstance(resp.data, dict) or not isinstance(resp.data.get('quote'), dict):
            raise ConnectionError(f'quote unavailable: HTTP {resp.status}')
        return resp.data['quote']

    def list_positions(self) -> dict[str, Decimal]:
        resp = self._client.trading_get("/v2/positions", purpose="positions")
        if not resp.ok or not isinstance(resp.data, list):
            raise ConnectionError(f"/v2/positions failed: HTTP {resp.status}")
        return positions_from_alpaca(resp.data)

    def get_account(self) -> dict:
        resp = self._client.trading_get("/v2/account", purpose="account")
        if not resp.ok or not isinstance(resp.data, dict):
            raise ConnectionError(f"/v2/account failed: HTTP {resp.status}")
        return resp.data
