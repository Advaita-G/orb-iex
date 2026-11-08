"""Local broker simulator. Used by `shadow` mode and by the tests.

It implements the same interface as AlpacaPaperBroker and emits the same
OrderUpdate snapshots (cumulative filled_qty) into the event queue, so the
engine code path is identical in shadow, tests and paper.

Fill model (approximates the QC backtest fills, costs are modeled separately):
- stop order: triggers on a 1-minute IEX bar whose high >= stop (buy) or
  low <= stop (sell); fills in full at max(stop, bar open) / min(stop, bar open);
- market order: fills at the last known IEX price, otherwise at the next bar open.
Alpaca-like behaviour the tests need can be switched on: rejected submits or
replaces, cancels that stay pending_cancel, manual (partial) fills, orders that
start in pending_new (and refuse a replace until activated, like Alpaca), stop
prices checked against a quote (422 when already crossed) and lost submit replies.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from .broker import OrderRejected, OrderUncertain
from .marketdata import Bar
from .orders import TERMINAL, OrderRequest, OrderUpdate, parse_client_order_id

UNREPLACEABLE = frozenset({"accepted", "pending_new", "pending_cancel", "pending_replace", "partially_filled"})


class SimBroker:
    def __init__(self, sink: Callable[[OrderUpdate], None], equity: Decimal = Decimal("100000"),
                 clock: Callable[[], datetime] | None = None, auto_fill_market: bool = True):
        self.sink = sink
        self.equity = equity
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.auto_fill_market = auto_fill_market
        self.orders: dict[str, OrderUpdate] = {}
        self.created: dict[str, datetime] = {}
        self.positions: dict[str, Decimal] = {}
        self.last_price: dict[str, Decimal] = {}
        self.cash_pnl = Decimal(0)
        self._ids = itertools.count(1)
        # test knobs
        self.reject_roles: set[str] = set()
        self.reject_replace = False
        self.cancel_mode = "immediate"       # or "pending"
        self.pending_cancels: set[str] = set()
        self.initial_status = "new"          # or "pending_new": call activate() to move it to new
        self.quote_fn: Callable[[str], object] | None = None   # symbol -> Quote (bid/ask) for stop checks
        self.check_protective_stops = False  # also 422 a crossed protective stop (entries are always checked)
        self.timeout_roles: set[str] = set() # create the order, then lose the reply (OrderUncertain) once
        self.reject_role_counts: dict[str, int] = {}   # role -> refuse this many submits, then accept
        self.cancel_fail_count = 0           # refuse this many cancel requests (Alpaca error), then accept
        self.cancel_ignored: set[str] = set()  # order ids whose cancel is accepted but never takes effect
        self.limit_prices: dict[str, Decimal] = {}
        self.replace_rejected_state = 0      # replaces refused because the order was not replaceable
        self.crossed_rejections = 0          # 422 crossed-stop rejections
        self.max_live_stops: dict[str, int] = {}
        self.shortable: dict[str, bool] = {}
        self.submitted: list[OrderRequest] = []
        self.write_log: list[tuple] = []

    # -- helpers -------------------------------------------------------------
    def _emit(self, o: OrderUpdate, event: str) -> None:
        o.timestamp = self.clock().isoformat()
        snap = replace(o, event=event, source="sim", timestamp=self.clock().isoformat())
        self.sink(snap)

    def _snapshot(self, o: OrderUpdate) -> OrderUpdate:
        return replace(o, source="submit")

    # -- broker interface ------------------------------------------------------
    def submit_order(self, req: OrderRequest) -> OrderUpdate:
        self.write_log.append(("submit", req.client_order_id))
        self.submitted.append(req)
        if any(o.client_order_id == req.client_order_id for o in self.orders.values()):
            existing = next(o for o in self.orders.values() if o.client_order_id == req.client_order_id)
            return self._snapshot(existing)
        parsed = parse_client_order_id(req.client_order_id)
        role = parsed[2] if parsed else ""
        if role in self.reject_roles:
            raise OrderRejected(f"simulated rejection of {role}", 403)
        if self.reject_role_counts.get(role, 0) > 0:
            self.reject_role_counts[role] -= 1
            raise OrderRejected(f"simulated rejection of {role}", 403)
        self._check_stop_price(req, role)
        oid = f"sim-{next(self._ids)}"
        o = OrderUpdate(order_id=oid, client_order_id=req.client_order_id, symbol=req.symbol,
                        side=req.side, type=req.type, qty=Decimal(req.qty), status=self.initial_status,
                        stop_price=req.stop_price)
        self.orders[oid] = o
        self.created[oid] = self.clock()
        if req.limit_price is not None:
            self.limit_prices[oid] = Decimal(req.limit_price)
        self._emit(o, self.initial_status)
        snap = self._snapshot(o)
        if role == "stop":
            live = sum(1 for x in self.orders.values() if x.symbol == req.symbol and x.status not in TERMINAL
                       and (parse_client_order_id(x.client_order_id) or (None, None, ""))[2] == "stop")
            self.max_live_stops[req.symbol] = max(self.max_live_stops.get(req.symbol, 0), live)
        if role in self.timeout_roles:
            self.timeout_roles.discard(role)
            raise OrderUncertain(f"submit {req.client_order_id} got no reply and is not found")
        if req.type == "market" and self.auto_fill_market and req.symbol in self.last_price:
            self.fill(oid, o.qty, self.last_price[req.symbol])
        elif req.type == "limit" and self.auto_fill_market and req.symbol in self.last_price:
            last = self.last_price[req.symbol]          # a marketable limit fills at the last price
            if (req.side == "buy" and req.limit_price >= last) or (req.side == "sell" and req.limit_price <= last):
                self.fill(oid, o.qty, last)
        return snap

    def _check_stop_price(self, req: OrderRequest, role: str) -> None:
        """Alpaca refuses a stop whose price is already crossed (HTTP 422)."""
        if req.type != "stop" or req.stop_price is None or self.quote_fn is None:
            return
        if role != "entry" and not self.check_protective_stops:
            return
        q = self.quote_fn(req.symbol)
        if q is None:
            return
        one_cent = Decimal("0.01")
        if req.side == "buy" and q.ask is not None and req.stop_price < q.ask + one_cent:
            self.crossed_rejections += 1
            raise OrderRejected(f"HTTP 422: stop price must be greater than or equal to base price + 0.01 "
                                f"(stop {req.stop_price}, ask {q.ask})", 422)
        if req.side == "sell" and q.bid is not None and req.stop_price > q.bid - one_cent:
            self.crossed_rejections += 1
            raise OrderRejected(f"HTTP 422: stop price must be less than or equal to base price - 0.01 "
                                f"(stop {req.stop_price}, bid {q.bid})", 422)

    def activate(self, order_id: str | None = None) -> None:
        """pending_new -> new (what Alpaca does a moment after accepting an order)."""
        for o in list(self.orders.values()):
            if o.status == "pending_new" and order_id in (None, o.order_id):
                o.status = "new"
                self._emit(o, "new")

    def replace_order(self, order_id: str, qty: Decimal, client_order_id: str,
                      stop_price: Decimal | None = None) -> OrderUpdate:
        self.write_log.append(("replace", order_id, client_order_id))
        old = self.orders.get(order_id)
        if old is not None and old.status in UNREPLACEABLE:
            self.replace_rejected_state += 1
            raise OrderRejected(f"HTTP 422: cannot replace order in {old.status}", 422)
        if old is None or old.status in TERMINAL:
            raise OrderRejected("HTTP 422: order is not replaceable", 422)
        if self.reject_replace:
            raise OrderRejected("simulated replace rejection", 403)
        oid = f"sim-{next(self._ids)}"
        new = replace(old, order_id=oid, client_order_id=client_order_id, qty=Decimal(qty), status=self.initial_status,
                      stop_price=old.stop_price if stop_price is None else Decimal(stop_price),
                      filled_qty=Decimal(0), filled_avg_price=None, replaces=order_id, replaced_by=None)
        old.status, old.replaced_by = "replaced", oid
        self.orders[oid] = new
        self.created[oid] = self.clock()
        self._emit(old, "replaced")
        self._emit(new, self.initial_status)
        live = sum(1 for x in self.orders.values() if x.symbol == new.symbol and x.status not in TERMINAL
                   and (parse_client_order_id(x.client_order_id) or (None, None, ""))[2] == "stop")
        self.max_live_stops[new.symbol] = max(self.max_live_stops.get(new.symbol, 0), live)
        return self._snapshot(new)

    def cancel_order(self, order_id: str) -> None:
        self.write_log.append(("cancel", order_id))
        o = self.orders.get(order_id)
        if o is None:
            raise OrderRejected("order not found", 404)
        if o.status in TERMINAL:
            raise OrderRejected("order is not cancelable", 422)
        if self.cancel_fail_count > 0:
            self.cancel_fail_count -= 1
            raise OrderRejected("simulated cancel failure", 500)
        if order_id in self.cancel_ignored:
            return                                        # accepted, but the order stays live
        if self.cancel_mode == "pending":
            self.pending_cancels.add(order_id)
            o.status = "pending_cancel"
            self._emit(o, "pending_cancel")
        else:
            o.status = "canceled"
            self._emit(o, "canceled")

    def complete_cancels(self) -> None:
        for o in list(self.orders.values()):
            if o.order_id in self.pending_cancels and o.status not in TERMINAL:
                o.status = "canceled"
                self._emit(o, "canceled")
        self.pending_cancels.clear()

    def get_order_by_client_id(self, cid: str) -> OrderUpdate | None:
        for o in self.orders.values():
            if o.client_order_id == cid:
                return self._snapshot(o)
        return None

    def list_orders(self, status: str, after: str | None = None) -> list[OrderUpdate]:
        out = [self._snapshot(o) for o in self.orders.values()]
        if status == "open":
            out = [o for o in out if o.status not in TERMINAL]
        return out

    def list_positions(self) -> dict[str, Decimal]:
        return {s: q for s, q in self.positions.items() if q != 0}

    def latest_quote(self, symbol: str) -> dict:
        if self.quote_fn is not None:
            q = self.quote_fn(symbol)
            if q is None:
                raise ConnectionError('quote unavailable')
            return {'t': (q.t or self.clock()).isoformat(), 'bp': str(q.bid), 'ap': str(q.ask)}
        price = self.last_price.get(symbol)
        if price is None:
            entries = [o for o in self.orders.values() if o.symbol == symbol and o.filled_avg_price is not None]
            price = entries[-1].filled_avg_price if entries else Decimal('50')
        return {'t': self.clock().isoformat(), 'bp': str(price), 'ap': str(price)}

    def get_account(self) -> dict:
        return {"equity": str(self.equity + self.mark_to_market()), "buying_power": str(4 * self.equity),
                "account_number": "PASIM0000", "status": "ACTIVE", "shorting_enabled": True}

    # -- fills -----------------------------------------------------------------
    def fill(self, order_id: str, qty: Decimal, price: Decimal, emit: bool = True) -> OrderUpdate:
        o = self.orders[order_id]
        qty = Decimal(qty)
        remaining = o.qty - o.filled_qty
        qty = min(qty, remaining)
        if qty <= 0:
            return o
        prev_value = (o.filled_avg_price or Decimal(0)) * o.filled_qty
        o.filled_qty += qty
        o.filled_avg_price = (prev_value + qty * Decimal(price)) / o.filled_qty
        o.status = "filled" if o.filled_qty >= o.qty else "partially_filled"
        o.timestamp = self.clock().isoformat()
        signed = qty if o.side == "buy" else -qty
        pos = self.positions.get(o.symbol, Decimal(0))
        # cash flow; mark_to_market() adds open positions back at the last price
        self.cash_pnl -= signed * Decimal(price)
        self.positions[o.symbol] = pos + signed
        if self.positions[o.symbol] == 0:
            self.positions.pop(o.symbol)
        if emit:
            self._emit(o, "fill" if o.status == "filled" else "partial_fill")
        return o

    def on_bar(self, symbol: str, bar: Bar) -> None:
        """Process one completed 1-minute bar for `symbol`."""
        for o in list(self.orders.values()):
            if o.symbol != symbol or o.status in TERMINAL or o.status == "pending_cancel":
                continue
            if self.created.get(o.order_id) and bar.t < self.created[o.order_id].replace(second=0, microsecond=0):
                continue
            remaining = o.qty - o.filled_qty
            if o.type == "market":
                self.fill(o.order_id, remaining, bar.o)
            elif o.type == "limit" and self.limit_prices.get(o.order_id) is not None:
                lim = self.limit_prices[o.order_id]
                if o.side == "buy" and bar.l <= lim:
                    self.fill(o.order_id, remaining, min(lim, bar.o))
                elif o.side == "sell" and bar.h >= lim:
                    self.fill(o.order_id, remaining, max(lim, bar.o))
            elif o.type == "stop" and o.stop_price is not None:
                if o.side == "buy" and bar.h >= o.stop_price:
                    self.fill(o.order_id, remaining, max(o.stop_price, bar.o))
                elif o.side == "sell" and bar.l <= o.stop_price:
                    self.fill(o.order_id, remaining, min(o.stop_price, bar.o))
        self.last_price[symbol] = bar.c

    def mark_to_market(self) -> Decimal:
        return self.cash_pnl + sum(q * self.last_price.get(s, Decimal(0)) for s, q in self.positions.items())
