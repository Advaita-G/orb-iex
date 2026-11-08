"""Order model shared by the broker, stream, simulator and engine.

client_order_id is deterministic: orbiex-YYYYMMDD-SYMBOL-role
  roles: entry, stop1, stop2... (each replacement is a new id), and exits
  (lateexit1, stopfail1, stopupd1, eod1, opensweep1, closesweep1, flatten1)
The same id is sent again on a retry, so Alpaca itself refuses a duplicate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from . import constants as C

TERMINAL = frozenset({"filled", "canceled", "expired", "replaced", "rejected", "done_for_day"})
OPEN_LIKE = frozenset({"new", "accepted", "pending_new", "partially_filled", "pending_cancel",
                       "pending_replace", "accepted_for_bidding", "held", "calculated", "stopped",
                       "suspended"})
LOST = frozenset({"canceled", "expired", "rejected", "done_for_day"})

EXIT_ROLES = ("lateexit", "stopfail", "stopupd", "eod", "opensweep", "closesweep", "flatten", "postclose")
CID_RE = re.compile(rf"^{C.CLIENT_ORDER_PREFIX}-(\d{{8}})-([A-Z.]+)-([a-z]+)(\d*)$")


def client_order_id(day: date, symbol: str, role: str, seq: int | None = None) -> str:
    return f"{C.CLIENT_ORDER_PREFIX}-{day.strftime('%Y%m%d')}-{symbol}-{role}{seq if seq else ''}"


def parse_client_order_id(cid: str | None) -> tuple[date, str, str] | None:
    m = CID_RE.match(cid or "")
    if not m:
        return None
    d = datetime.strptime(m.group(1), "%Y%m%d").date()
    return d, m.group(2), m.group(3)


def role_kind(role: str) -> str:
    if role == "entry":
        return "entry"
    if role == "stop":
        return "stop"
    if role in EXIT_ROLES:
        return "exit"
    return "unknown"


def status_rank(status: str) -> int:
    if status in TERMINAL:
        return 3
    if status in ("partially_filled", "pending_cancel", "pending_replace"):
        return 2
    return 1


@dataclass
class OrderRequest:
    symbol: str
    side: str                  # buy / sell
    qty: Decimal
    type: str                  # stop / market
    client_order_id: str
    stop_price: Decimal | None = None
    time_in_force: str = "day"
    limit_price: Decimal | None = None      # only for the after-close flatten (marketable limit)
    extended_hours: bool = False

    def payload(self) -> dict:
        p = {"symbol": self.symbol, "side": self.side, "qty": str(self.qty), "type": self.type,
             "time_in_force": self.time_in_force, "client_order_id": self.client_order_id}
        if self.stop_price is not None:
            p["stop_price"] = str(self.stop_price)
        if self.limit_price is not None:
            p["limit_price"] = str(self.limit_price)
        if self.extended_hours:
            p["extended_hours"] = True
        return p


@dataclass
class OrderUpdate:
    """One order snapshot, from the stream, a REST poll, a submit reply or the simulator.
    filled_qty is CUMULATIVE, which makes processing idempotent."""
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    type: str
    qty: Decimal
    status: str
    filled_qty: Decimal = Decimal(0)
    filled_avg_price: Decimal | None = None
    stop_price: Decimal | None = None
    replaced_by: str | None = None
    replaces: str | None = None
    event: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source: str = "rest"


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    return Decimal(str(v))


def from_alpaca_order(o: dict, event: str | None = None, source: str = "rest",
                      timestamp: str | None = None) -> OrderUpdate:
    return OrderUpdate(
        order_id=str(o["id"]), client_order_id=str(o.get("client_order_id") or ""),
        symbol=str(o.get("symbol") or ""), side=str(o.get("side") or ""),
        type=str(o.get("type") or o.get("order_type") or ""), qty=_dec(o.get("qty")) or Decimal(0),
        status=str(o.get("status") or ""), filled_qty=_dec(o.get("filled_qty")) or Decimal(0),
        filled_avg_price=_dec(o.get("filled_avg_price")), stop_price=_dec(o.get("stop_price")),
        replaced_by=o.get("replaced_by"), replaces=o.get("replaces"), event=event,
        timestamp=timestamp or o.get("updated_at") or datetime.now(timezone.utc).isoformat(),
        source=source)


def from_trade_update(msg: dict) -> OrderUpdate | None:
    """Alpaca trade_updates message data -> OrderUpdate."""
    data = msg.get("data") if "data" in msg else msg
    if not isinstance(data, dict) or not isinstance(data.get("order"), dict):
        return None
    return from_alpaca_order(data["order"], event=data.get("event"), source="stream",
                             timestamp=data.get("timestamp"))
