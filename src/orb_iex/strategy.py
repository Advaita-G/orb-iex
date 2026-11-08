"""Scan, ranking, direction, entry levels and sizing (Main.cs OnData + PlaceEntry).

Everything here is pure: inputs in, decisions out. No network, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from . import constants as C
from .indicators import atr_sma, average_daily_volume, relative_volume
from .marketdata import Bar

CENT = Decimal("0.01")


@dataclass
class Features:
    symbol: str
    opening: Bar | None           # today's 09:30-09:35 IEX bar
    relvol: Decimal | None
    atr: Decimal | None
    adv: Decimal | None
    opening_history: int = 0      # previous opening bars available
    daily_history: int = 0
    exclusion_reason: str | None = None

    @property
    def ready(self) -> bool:
        # Main.cs IsReadyFor: opening bar is today's, RelVol defined, ATR and ADV ready
        return (self.exclusion_reason is None and self.opening is not None and self.relvol is not None
                and self.atr is not None and self.adv is not None)

    def row(self) -> dict:
        o = self.opening
        return {"symbol": self.symbol, "relvol": self.relvol, "atr": self.atr, "adv": self.adv,
                "open": o and o.o, "high": o and o.h, "low": o and o.l, "close": o and o.c,
                "volume": o and o.v, "opening_history": self.opening_history,
                "daily_history": self.daily_history, "history_exclusion": self.exclusion_reason}


def compute_features(symbol: str, today_bar: Bar | None, daily: dict[date, Bar],
                     openings: dict[date, Bar], today: date) -> Features:
    """daily/openings: bars of sessions before today (full history, no warm-up wait)."""
    all_daily = [daily[d] for d in sorted(daily) if d < today]
    prev_open = [openings[d].v for d in sorted(openings) if d < today]
    atr = atr_sma(all_daily, C.LOOKBACK_DAYS)
    adv = average_daily_volume(all_daily, C.LOOKBACK_DAYS)
    rv = relative_volume(today_bar.v, prev_open, C.LOOKBACK_DAYS) if today_bar else None
    return Features(symbol, today_bar, rv, atr, adv, len(prev_open), len(all_daily))


def reject_reason(f: Features) -> str | None:
    if f.exclusion_reason:
        return f.exclusion_reason
    if not f.ready:
        missing = [n for n, v in (("opening_bar", f.opening), ("relvol", f.relvol),
                                  ("atr", f.atr), ("adv", f.adv)) if v is None]
        return "not_ready:" + ",".join(missing)
    if not f.opening.o > C.MIN_PRICE:
        return "open<=5"
    if not f.relvol >= C.MIN_RELATIVE_VOLUME:
        return "relvol<1"
    if not f.atr > C.MIN_ATR:
        return "atr<=0.50"
    if not f.adv >= C.MIN_AVERAGE_VOLUME:
        return "adv<1M"
    return None


def select_candidates(features: list[Features]) -> tuple[list[Features], list[dict]]:
    """Filter, rank by RelVol desc then ticker asc, take the top 20.
    Returns (top20, table rows for every symbol). No backfill happens later:
    the caller only ever works through this fixed top-20 list."""
    table, passed = [], []
    for f in features:
        reason = reject_reason(f)
        if reason:
            table.append({"symbol": f.symbol, "status": "filtered", "reason": reason, "details": f.row()})
        else:
            passed.append(f)
    passed.sort(key=lambda f: (-f.relvol, f.symbol))
    top = passed[:C.MAX_POSITIONS]
    for i, f in enumerate(passed, start=1):
        in_top = i <= C.MAX_POSITIONS
        table.append({"symbol": f.symbol, "rank": i, "status": "candidate" if in_top else "below_top20",
                      "reason": None if in_top else "rank>20", "details": f.row()})
    table.sort(key=lambda r: (r.get("rank") or 10**6, r["symbol"]))
    return top, table


@dataclass
class EntryPlan:
    symbol: str
    direction: int                 # +1 long, -1 short
    entry_price: Decimal           # rounded order price
    stop_price: Decimal            # rounded protective stop
    raw_entry: Decimal
    raw_stop: Decimal
    atr: Decimal
    qty: int                       # unsigned whole shares
    sizing: dict = field(default_factory=dict)

    @property
    def side(self) -> str:
        return "buy" if self.direction > 0 else "sell"


def direction_and_levels(f: Features) -> tuple[int, Decimal, Decimal] | str:
    """Main.cs: close > open long at the high; close < open short at the low."""
    b = f.opening
    if b.c > b.o:
        return 1, b.h, b.h - C.STOP_ATR_FRACTION * f.atr
    if b.c < b.o:
        return -1, b.l, b.l + C.STOP_ATR_FRACTION * f.atr
    return "flat_bar"


def round_levels(direction: int, entry: Decimal, stop: Decimal) -> tuple[Decimal, Decimal]:
    """Alpaca rejects sub-penny prices above $1 (DIVERGENCES AL-11). Entry is
    rounded away from the market (never triggers earlier than the range
    break); the protective stop is rounded to the nearest cent."""
    if direction > 0:
        e = entry.quantize(CENT, rounding=ROUND_CEILING)
    else:
        e = entry.quantize(CENT, rounding=ROUND_FLOOR)
    return e, stop.quantize(CENT, rounding=ROUND_HALF_UP)


def lean_calculate_order_quantity(equity: Decimal, weight: Decimal, price: Decimal,
                                  commission_per_share: Decimal) -> int:
    """Approximation of LEAN CalculateOrderQuantity(symbol, weight) for an empty
    holding: target value = (TPV - free buffer 0.25%) * weight, whole shares at
    the current price, reduced until shares + modeled fees fit the target."""
    if price <= 0:
        return 0
    target = equity * (1 - C.LEAN_FREE_PORTFOLIO_PCT) * weight
    qty = int((target / price).to_integral_value(rounding=ROUND_FLOOR))
    while qty > 0 and qty * price + qty * commission_per_share > target:
        qty -= 1
    return max(qty, 0)


def size_position(equity: Decimal, max_gross: Decimal, entry: Decimal, stop_distance: Decimal,
                  current_price: Decimal, commission_per_share: Decimal) -> tuple[int, dict]:
    allocation = equity / C.MAX_POSITIONS
    risk_qty = int((allocation * C.RISK_FRACTION_OF_ALLOCATION / stop_distance)
                   .to_integral_value(rounding=ROUND_FLOOR))
    weight = max_gross / C.MAX_POSITIONS      # paper 4x cap shared by 20 positions
    calc_qty = lean_calculate_order_quantity(equity, weight, current_price, commission_per_share)
    cap_qty = int((equity * weight / entry).to_integral_value(rounding=ROUND_FLOOR))
    qty = max(min(risk_qty, calc_qty, cap_qty), 0)
    binding = min((("risk", risk_qty), ("weight_calc", calc_qty), ("notional_cap", cap_qty)),
                  key=lambda kv: kv[1])[0]
    return qty, {"equity": equity, "allocation": allocation, "risk_qty": risk_qty, "weight": weight,
                 "weight_calc_qty": calc_qty, "notional_cap_qty": cap_qty, "qty": qty, "binding": binding,
                 "stop_distance": stop_distance}


def plan_entry(f: Features, equity: Decimal, max_gross: Decimal,
               commission_per_share: Decimal) -> EntryPlan | str:
    """Paper entry: stop order at the range high/low, even if the price is already
    there. Broker checks (position/open order, shortability, buying power) happen
    at submit. stop_price here is only the plan; the live protective stop is set
    from the actual average fill price (engine)."""
    levels = direction_and_levels(f)
    if isinstance(levels, str):
        return levels
    direction, raw_entry, raw_stop = levels
    price = f.opening.c   # IEX-5: stands in for QC Security.Price (09:34 close)
    entry, stop = round_levels(direction, raw_entry, raw_stop)
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return "zero_stop_distance"
    qty, sizing = size_position(equity, max_gross, entry, stop_distance, price, commission_per_share)
    raw_distance = abs(raw_entry - raw_stop)
    if raw_distance > 0:
        ref_qty, _ = size_position(equity, max_gross, raw_entry, raw_distance, price, commission_per_share)
        sizing["reference_qty_unrounded"] = ref_qty
    if qty == 0:
        return "zero_quantity"
    return EntryPlan(f.symbol, direction, entry, stop, raw_entry, raw_stop, f.atr, qty, sizing)
