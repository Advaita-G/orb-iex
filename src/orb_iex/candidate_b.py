"""Frozen liquidity hypothesis; no orders, indicators, tuning, or outcome inputs.

IEX sizes are round-lot counts. Without point-in-time lot definitions we use
the count itself as a conservative lower bound on shares, never multiply by 100.
This cannot establish consolidated depth or a future stop fill.
"""
from dataclasses import replace
from decimal import Decimal, ROUND_FLOOR

from .strategy import EntryPlan, Features

OPENING_PARTICIPATION = Decimal("0.01")
MAX_COST_RISK = Decimal("0.25")


def adapt_plan(plan: EntryPlan, feature: Features) -> EntryPlan | str:
    bar = feature.opening
    if bar is None or bar.v <= 0:
        return "candidate_missing_opening_volume"
    cap = int((Decimal(bar.v) * OPENING_PARTICIPATION).to_integral_value(rounding=ROUND_FLOOR))
    qty = min(plan.qty, cap)
    if qty <= 0:
        return "candidate_zero_liquidity_quantity"
    sizing = {**plan.sizing, "baseline_qty": plan.qty, "opening_volume": bar.v,
              "opening_participation_cap": cap, "qty": qty,
              "binding": "opening_volume" if qty < plan.qty else plan.sizing["binding"]}
    return replace(plan, qty=qty, sizing=sizing)


def liquidity_check(q, *, direction: int, qty: int, risk: Decimal,
                    commission: Decimal, slippage: Decimal) -> dict:
    """Call only after the existing quote age, price, spread and trigger checks.

    Full spread plus two-sided modeled costs is a conservative screening budget,
    not a measured cost or a guarantee of execution at the trigger.
    """
    cost = q.ask - q.bid + 2 * (commission + slippage)
    size = q.ask_size if direction > 0 else q.bid_size
    reason = None
    if risk <= 0 or cost > MAX_COST_RISK * risk:
        reason = "candidate_cost_risk"
    elif size is None or size < qty:
        reason = "candidate_displayed_size"
    return {"reason": reason, "candidate_qty": qty, "planned_risk_per_share": str(risk),
            "screen_cost_per_share": str(cost), "max_cost_risk": str(MAX_COST_RISK),
            "entry_size_round_lots": size, "size_share_lower_bound": size,
            "size_unit_policy": "one_share_lower_bound_per_round_lot"}
