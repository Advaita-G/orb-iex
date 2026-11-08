"""Trade reconstruction with split accounting and a full lifecycle trace.

A **trade is a bot-owned position from flat to flat**, across session days. One
trade can contain many partial fills, order replacements, stop changes and
separate entry/exit orders; every one of them stays visible as raw evidence
underneath the trade.

The grouping is the same definition as :func:`orb_iex.reconcile.reconstruct`
(a fill that crosses zero is split into a close and a new open, flagged
``sign_flip``; positions still open are reported, never dropped). A unit test
pins the two implementations to the same trips, so the dashboard and the bot's
own reconciliation can never disagree about what a trade is.

Accounting is kept in separate columns and never mixed:

``realized_pnl``        cash result of the real PAPER fill prices (real slippage is
                        already inside those prices, so it is never added again)
``recorded_fees``       actual broker fees. Alpaca paper charges none, so this is
                        ``None`` with a reason, never ``0``.
``modeled_cost``        the bot's own per-share cost model, summed from the values the
                        engine recorded on each fill (fallback: the configured rates)
``modeled_net_pnl``     ``realized_pnl - modeled_cost``
``r``                   ``realized_pnl / initial_risk``. ``initial_risk`` is FROZEN at
                        plan time (the trade plan's ``one_r_per_share``, else
                        ``STOP_ATR_FRACTION * ATR``); a stop that was moved later
                        never changes R.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .. import constants as C
from ..tradelog import EXIT_REASON, RELVOL_BUCKETS

D = Decimal
ZERO = D(0)

# -- flags -------------------------------------------------------------------
# Execution DEFECTS: the strategy said one thing and the plumbing did another.
# These are the flags that make an outcome "not the strategy's fault".
EXECUTION_DEFECTS = {
    "stop_rejected", "stop_update_failed", "stop_coverage_short", "entry_unprotected",
    "flatten_without_stop", "after_close_flatten", "sign_flip", "unmatched_exit_fill",
    "entry_crossed_skip", "entry_rejected_unfilled", "entry_rejected_partial",
    "exit_late", "unresolved_intent", "entry_late",
}
# Execution EVENTS: normal mechanics worth naming, but not failures. They never
# change the loss classification (a partial fill of a stop order is how the market
# worked that moment, not a defect).
EXECUTION_EVENTS = {
    "entry_partial_fill", "exit_partial_fill", "stop_replaced", "stop_filled_then_flatten",
    "eod_exit", "carry_over", "recovery_event", "duplicate_prevention",
}
# Broker-side: the broker failed, refused or answered ambiguously.
BROKER_FLAGS = {
    "order_rejected", "broker_error", "http_error", "ambiguous_response", "stream_reconnect",
    "broker_position_mismatch", "stale_order_snapshot",
}
# Decision DATA problems: an input the signal depended on was missing, stale or unusable.
DATA_FLAGS = {"quote_stale", "quote_unavailable", "spread_too_wide", "history_gap", "bar_diverged"}
# RECORD gaps: the outcome is known, the paper trail around it is incomplete. Shown
# everywhere, folded into no classification, because they describe the record and not
# the trading.
RECORD_GAPS = {"no_plan", "no_candidate", "no_fills", "incomplete_round_trip", "no_diagnostics"}
LOSS_CLASSES = ("normal_strategy", "execution_defect", "data_issue", "broker_issue", "mixed", "unknown")

NARRATIVE_ROLES = ("flatten", "close", "postclose", "lateexit", "stopfail", "stopupd")


def bar_diverged(diagnostic: dict | None, symbol: str) -> bool:
    """True when the 09:37 diagnostic re-read of the opening bars disagreed for this symbol.

    The bot journals that comparison (``diagnostics.kind='divergence'``) and never trades
    on it: a disagreement means the 09:30-09:35 IEX bar behind the signal was not
    reproducible two minutes later, so any outcome built on it is not trustworthy.
    """
    body = (diagnostic or {}).get("body")
    if not isinstance(body, dict):
        return False
    if symbol in (body.get("appeared_late") or []) or symbol in (body.get("vanished") or []):
        return True
    return symbol in (body.get("changed") or {})


def q(v: Decimal | None, places: str = "0.01") -> str | None:
    """Quantize for display; never render '-0.00'."""
    if v is None:
        return None
    value = D(v).quantize(D(places))
    return str(value.copy_abs() if value == 0 else value)


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return D(str(v))
    except (InvalidOperation, ValueError):
        return None


def _mean(xs: list[Decimal]) -> Decimal | None:
    return sum(xs, ZERO) / len(xs) if xs else None


def signed_qty(fill: dict) -> Decimal:
    """Signed shares of one fill row (buy +, sell -)."""
    value = _dec(fill.get("qty")) or ZERO
    return value if fill.get("side") == "buy" else -value


def normalise_fills(rows: Iterable[dict], default_commission: Decimal = ZERO,
                    default_slippage: Decimal = ZERO) -> list[dict]:
    """State-journal fill rows -> ordered fill increments with signed quantity and cost.

    Rows without a price or quantity are dropped (they carry no accounting value);
    the count of dropped rows is reported by the caller, never hidden.
    """
    out: list[dict] = []
    for r in rows:
        qty = _dec(r.get("qty"))
        price = _dec(r.get("price"))
        if qty is None or qty <= 0 or price is None:
            continue
        commission = _dec(r.get("commission"))
        slippage = _dec(r.get("slippage"))
        out.append({
            "order_id": r.get("order_id"), "symbol": r.get("symbol"), "role": r.get("role"),
            "side": r.get("side"), "qty": qty, "price": price, "ts": r.get("ts"),
            "cum_qty": r.get("cum_qty"),
            "commission_per_share": (commission / qty) if commission is not None else default_commission,
            "slippage_per_share": (slippage / qty) if slippage is not None else default_slippage,
        })
    out.sort(key=lambda f: (f["ts"] or "", f["order_id"] or "", str(f["cum_qty"] or "")))
    return out


def _new_trip(symbol: str, direction: int, first: dict) -> dict:
    return {"symbol": symbol, "direction": direction, "entry_fills": [], "exit_fills": [],
            "sign_flip": False, "entry_time": first["ts"], "exit_time": None,
            "order_ids": set(), "roles": []}


def _finish(trip: dict, default_commission: Decimal, default_slippage: Decimal,
            risk_per_share: Decimal | None) -> dict:
    entries, exits = trip["entry_fills"], trip["exit_fills"]
    eq = sum(f["qty"] for f in entries)
    xq = sum(f["qty"] for f in exits)
    ev = sum(f["qty"] * f["price"] for f in entries) / eq if eq else None
    xv = sum(f["qty"] * f["price"] for f in exits) / xq if xq else None
    direction = trip["direction"]
    matched = min(eq, xq)
    gross = direction * (xv - ev) * matched if (xv is not None and ev is not None) else None
    parts = entries + exits
    commission = sum((f["commission_per_share"] * f["qty"] for f in parts), ZERO)
    slippage = sum((f["slippage_per_share"] * f["qty"] for f in parts), ZERO)
    config_cost = (default_commission + default_slippage) * (eq + xq)
    risk = (risk_per_share * eq) if (risk_per_share and eq) else None
    roles: dict[str, Decimal] = {}
    for f in exits:
        roles[f["role"]] = roles.get(f["role"], ZERO) + f["qty"]
    return {
        "symbol": trip["symbol"], "day": (trip["entry_time"] or "")[:10], "direction": direction,
        "qty": eq, "exit_qty": xq, "matched_qty": matched, "open_qty": eq - xq,
        "entry_vwap": ev, "exit_vwap": xv,
        "entry_time": trip["entry_time"], "exit_time": trip["exit_time"],
        "carry": bool(trip["exit_time"]) and trip["exit_time"][:10] != trip["entry_time"][:10],
        "sign_flip": trip["sign_flip"], "gross_pnl": gross, "realized_pnl": gross,
        "modeled_commission": commission, "modeled_slippage": slippage,
        "modeled_cost": commission + slippage, "modeled_cost_from_config": config_cost,
        "net_modeled_pnl": None if gross is None else gross - commission - slippage,
        "initial_risk": risk, "r": (gross / risk) if (gross is not None and risk) else None,
        "order_ids": sorted(trip["order_ids"]), "exit_roles": {k: str(v) for k, v in roles.items()},
        "entry_fills": entries, "exit_fills": exits, "roles": list(trip["roles"]),
    }


def group_fills(fills: list[dict], *, default_commission: Decimal = D("0.0035"),
                default_slippage: Decimal = D("0.01"),
                risk_per_share: dict[tuple[str, str], Decimal] | None = None
                ) -> tuple[list[dict], list[dict]]:
    """(closed, open) trips, flat to flat, in the same sense as reconcile.reconstruct."""
    risk_per_share = risk_per_share or {}
    pos: dict[str, Decimal] = {}
    cur: dict[str, dict] = {}
    closed: list[dict] = []
    opened: list[dict] = []
    for f in fills:
        symbol = f["symbol"]
        remaining = f["qty"]
        sign = D(1) if f["side"] == "buy" else D(-1)
        while remaining > 0:
            held = pos.get(symbol, ZERO)
            part = {**f, "qty": remaining}
            if held == ZERO or (held > 0) == (sign > 0):
                if held == ZERO:
                    cur[symbol] = _new_trip(symbol, int(sign), f)
                cur[symbol]["entry_fills"].append(part)
                cur[symbol]["order_ids"].add(f["order_id"])
                cur[symbol]["roles"].append(f["role"])
                pos[symbol] = held + sign * remaining
                remaining = ZERO
            else:
                take = min(remaining, abs(held))
                cur[symbol]["exit_fills"].append({**f, "qty": take})
                cur[symbol]["order_ids"].add(f["order_id"])
                cur[symbol]["roles"].append(f["role"])
                pos[symbol] = held + sign * take
                remaining -= take
                if pos[symbol] == ZERO:
                    trip = cur.pop(symbol)
                    trip["exit_time"] = f["ts"]
                    trip["sign_flip"] = remaining > 0
                    closed.append(_finish(trip, default_commission, default_slippage,
                                          risk_per_share.get((trip["entry_time"][:10], symbol))))
    for symbol, trip in cur.items():
        opened.append(_finish(trip, default_commission, default_slippage,
                              risk_per_share.get((trip["entry_time"][:10], symbol))))
    closed.sort(key=lambda t: (t["entry_time"] or "", t["symbol"]))
    opened.sort(key=lambda t: (t["entry_time"] or "", t["symbol"]))
    return closed, opened


# ---------------------------------------------------------------------------
# Trade object
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    """One position lifecycle, with every accounting view kept separate."""

    raw: dict
    sequence: int = 1
    symbol: str = ""
    day: str = ""
    strategy: str = C.VARIANT
    strategy_version: str | None = None
    git_commit: str | None = None
    run_id: str | None = None
    session_day: str | None = None
    planned_entry: Decimal | None = None
    planned_stop: Decimal | None = None
    final_stop: Decimal | None = None
    planned_qty: Decimal | None = None
    one_r_per_share: Decimal | None = None
    r_source: str = "unavailable"
    relvol: Decimal | None = None
    atr: Decimal | None = None
    adv: Decimal | None = None
    opening: dict[str, Any] | None = None
    sizing: dict[str, Any] | None = None
    state: str | None = None
    closed_flag: bool | None = None
    execution_defects: list[str] = field(default_factory=list)
    execution_events: list[str] = field(default_factory=list)
    broker_flags: list[str] = field(default_factory=list)
    data_flags: list[str] = field(default_factory=list)
    record_gaps: list[str] = field(default_factory=list)
    exit_reason_dominant: str | None = None
    plan_source: str = "unavailable"
    loss_class: str = "unknown"
    loss_reasons: list[str] = field(default_factory=list)
    recorded_fees: Decimal | None = None
    recorded_fees_note: str = "Alpaca paper charges no commission and no fees"
    exit_reason: str | None = None
    entry_slippage: Decimal | None = None
    exit_slippage: Decimal | None = None
    evidence_note: str | None = None

    # -- derived -------------------------------------------------------------
    @property
    def trade_id(self) -> str:
        suffix = f":{self.sequence}" if self.sequence > 1 else ""
        return f"{self.day}:{self.symbol}{suffix}"

    @property
    def side(self) -> str:
        return "long" if self.raw["direction"] > 0 else "short"

    @property
    def realized(self) -> Decimal | None:
        return self.raw["realized_pnl"]

    @property
    def net(self) -> Decimal | None:
        return self.raw["net_modeled_pnl"]

    @property
    def r(self) -> Decimal | None:
        return self.raw["r"]

    @property
    def holding_seconds(self) -> int | None:
        if not (self.raw["entry_time"] and self.raw["exit_time"]):
            return None
        try:
            a = datetime.fromisoformat(self.raw["entry_time"])
            b = datetime.fromisoformat(self.raw["exit_time"])
        except ValueError:
            return None
        return int((b - a).total_seconds())

    @property
    def mfe_r_proxy(self) -> Decimal | None:
        """Best excursion of the FILL prices only (1-minute bars are not in the journal).

        Labelled a proxy everywhere it is shown: it is a lower bound on MFE/MAE, not
        the true excursion, because the journal holds no intra-trade price series.
        """
        fills = self.raw["entry_fills"] + self.raw["exit_fills"]
        if not fills or self.one_r_per_share in (None, ZERO):
            return None
        prices = [f["price"] for f in fills]
        direction = self.raw["direction"]
        entry = self.raw["entry_vwap"]
        if entry is None:
            return None
        favourable = (max(prices) - entry) if direction > 0 else (entry - min(prices))
        return favourable / self.one_r_per_share

    @property
    def mae_r_proxy(self) -> Decimal | None:
        fills = self.raw["entry_fills"] + self.raw["exit_fills"]
        if not fills or self.one_r_per_share in (None, ZERO):
            return None
        prices = [f["price"] for f in fills]
        direction = self.raw["direction"]
        entry = self.raw["entry_vwap"]
        if entry is None:
            return None
        adverse = (entry - min(prices)) if direction > 0 else (max(prices) - entry)
        return adverse / self.one_r_per_share

    def relvol_bucket(self) -> str | None:
        if self.relvol is None:
            return None
        for lo, hi in RELVOL_BUCKETS:
            if self.relvol >= lo and (hi is None or self.relvol < hi):
                return f"{lo}-{hi}" if hi is not None else f"{lo}+"
        return None

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        raw = self.raw
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "strategy": self.strategy,
            "strategy_version": self.strategy_version,
            "git_commit": self.git_commit,
            "run_id": self.run_id,
            "day": self.day,
            "session_day": self.session_day,
            "side": self.side,
            "direction": raw["direction"],
            "entry_time": raw["entry_time"],
            "exit_time": raw["exit_time"],
            "holding_seconds": self.holding_seconds,
            "qty": str(raw["qty"]),
            "matched_qty": str(raw["matched_qty"]),
            "open_qty": str(raw["open_qty"]),
            "planned_qty": None if self.planned_qty is None else str(self.planned_qty),
            "entry_vwap": q(raw["entry_vwap"]),
            "exit_vwap": q(raw["exit_vwap"]),
            "planned_entry": q(self.planned_entry),
            "planned_stop": q(self.planned_stop),
            "final_stop": q(self.final_stop),
            "realized_pnl": q(raw["realized_pnl"]),
            "recorded_fees": q(self.recorded_fees),
            "recorded_fees_note": self.recorded_fees_note,
            "modeled_commission": q(raw["modeled_commission"], "0.0001"),
            "modeled_slippage": q(raw["modeled_slippage"]),
            "modeled_cost": q(raw["modeled_cost"]),
            "modeled_cost_from_config": q(raw["modeled_cost_from_config"]),
            "modeled_net_pnl": q(raw["net_modeled_pnl"]),
            "one_r_per_share": q(self.one_r_per_share, "0.0001"),
            "initial_risk": q(raw["initial_risk"]),
            "r_source": self.r_source,
            "plan_source": self.plan_source,
            "r": q(raw["r"], "0.01"),
            "r_available": raw["r"] is not None,
            "mfe_r_proxy": q(self.mfe_r_proxy, "0.01"),
            "mae_r_proxy": q(self.mae_r_proxy, "0.01"),
            "mfe_mae_source": "fill_prices_only",
            "entry_slippage_per_share": q(self.entry_slippage, "0.0001"),
            "entry_slippage_dollars": q(None if self.entry_slippage is None else
                                        self.entry_slippage * raw["qty"]),
            "exit_slippage_per_share": q(self.exit_slippage, "0.0001"),
            "exit_reason": self.exit_reason,
            "exit_reason_dominant": self.exit_reason_dominant,
            "exit_roles": raw["exit_roles"],
            "relvol": None if self.relvol is None else str(self.relvol),
            "relvol_bucket": self.relvol_bucket(),
            "atr": None if self.atr is None else str(self.atr),
            "adv": None if self.adv is None else str(self.adv),
            "opening": self.opening,
            "sizing": self.sizing,
            "state": self.state,
            "carry": raw["carry"],
            "sign_flip": raw["sign_flip"],
            "closed": (raw["open_qty"] == ZERO),
            "exit_qty": str(raw["exit_qty"]),
            "execution_defects": self.execution_defects,
            "execution_events": self.execution_events,
            "execution_flags": self.execution_defects + self.execution_events,
            "has_execution_defect": bool(self.execution_defects),
            "broker_flags": self.broker_flags,
            "data_flags": self.data_flags,
            "record_gaps": self.record_gaps,
            "loss_class": self.loss_class,
            "loss_reasons": self.loss_reasons,
            "order_ids": raw["order_ids"],
            "entry_fill_count": len(raw["entry_fills"]),
            "exit_fill_count": len(raw["exit_fills"]),
            "evidence_note": self.evidence_note,
            **({"raw_expanded": expand(raw)} if include_raw else {}),
        }


def expand(trip: dict) -> dict[str, Any]:
    """Raw fill increments behind a trade, in order. Never rounded, never summarised away."""
    def one(f):
        return {"order_id": f["order_id"], "role": f["role"], "side": f["side"], "qty": str(f["qty"]),
                "price": str(f["price"]), "ts": f["ts"], "cum_qty": f.get("cum_qty"),
                "commission_per_share": str(f["commission_per_share"]),
                "slippage_per_share": str(f["slippage_per_share"])}
    return {"entry_fills": [one(f) for f in trip["entry_fills"]],
            "exit_fills": [one(f) for f in trip["exit_fills"]]}


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

# Why the position closed, in priority order. A stop that fired and left a remainder
# to be flattened still reports 'stop': the stop is the reason, the flatten is cleanup.
EXIT_PRIORITY = ("stop", "eod", "post_close", "flatten", "late_fill_exit", "stop_failed_exit",
                 "stop_update_failed_exit", "open_sweep")


def _exit_reason(exit_roles: dict[str, str]) -> tuple[str | None, str | None]:
    """(reason, dominant). ``dominant`` is the role that closed the most shares."""
    if not exit_roles:
        return None, None
    mapped = {EXIT_REASON.get(role, role): D(qty) for role, qty in exit_roles.items()}
    dominant = max(mapped, key=lambda r: mapped[r])
    for reason in EXIT_PRIORITY:
        if reason in mapped:
            return reason, dominant
    return dominant, dominant


class TradeSet:
    """Every trade the bot has on record, rebuilt from raw fills, enriched from the journals."""

    def __init__(self, trades: list[Trade], open_trades: list[Trade], notes: list[str],
                 starting_equity: Decimal | None = None, equity_points: list[dict] | None = None,
                 dropped_fill_rows: int = 0):
        self.trades = trades
        self.open_trades = open_trades
        self.notes = notes
        self.starting_equity = starting_equity
        self.equity_points = equity_points or []
        self.dropped_fill_rows = dropped_fill_rows

    def by_id(self, trade_id: str) -> Trade | None:
        return next((t for t in self.trades + self.open_trades if t.trade_id == trade_id), None)


def build(sources, settings, mode: str = "paper") -> TradeSet:
    """Rebuild every trade from the state journal's fills, enriched from the trading journal."""
    notes: list[str] = []
    state = sources.state_journal
    tlog = sources.trade_journal
    logs = sources.logs

    raw_fills = state.fills()
    fills = normalise_fills(raw_fills, settings.commission_per_share, settings.slippage_per_share)
    dropped = len(raw_fills) - len(fills)
    if dropped:
        notes.append(f"{dropped} fill row(s) carried no price or quantity and are excluded from accounting")

    plans = {(p["day"], p["symbol"]): p for p in tlog.plans()}
    candidates = {(c["day"], c["symbol"]): c for c in tlog.candidates()}
    sj_trade_rows = state.trades()
    sj_trades = {(t["day"], t["symbol"]): t for t in sj_trade_rows}
    sj_candidates = {(c["day"], c["symbol"]): c["details"] for c in state.candidates()
                     if isinstance(c.get("details"), dict) and c["details"]}
    diagnostics = {(d["day"], d["kind"]): d for d in state.diagnostics(kind="divergence")}
    runs = {r["run_id"]: r for r in tlog.runs()}
    run_for_day: dict[str, str] = {}
    for c in tlog.candidates():
        run_for_day.setdefault(c["day"], c["run_id"])

    # R is computed from the FROZEN plan risk; the risk map is needed before grouping.
    risk_per_share: dict[tuple[str, str], Decimal] = {}
    for (day, symbol), plan in plans.items():
        one_r = _dec(plan.get("one_r_per_share"))
        if one_r and one_r > 0:
            risk_per_share[(day, symbol)] = one_r
    for (day, symbol), t in sj_trades.items():
        if (day, symbol) in risk_per_share:
            continue
        atr = _dec(t.get("atr"))
        if atr and atr > 0:
            risk_per_share[(day, symbol)] = C.STOP_ATR_FRACTION * atr

    closed, opened = group_fills(fills, default_commission=settings.commission_per_share,
                                 default_slippage=settings.slippage_per_share,
                                 risk_per_share=risk_per_share)

    orders_all = state.orders()
    orders_by_key: dict[tuple[str, str], list[dict]] = {}
    for o in orders_all:
        orders_by_key.setdefault((o["day"], o["symbol"]), []).append(o)
    intents = state.intents()
    intent_by_symbol: dict[str, list[dict]] = {}
    for i in intents:
        intent_by_symbol.setdefault(i["symbol"], []).append(i)

    log_days = set(logs.available_days())
    day_events: dict[str, list[dict]] = {}
    day_quotes: dict[str, dict[str, list[dict]]] = {}
    for day in sorted({t["day"] for t in closed + opened if t["day"]}):
        if day not in log_days:
            continue
        day_events[day] = logs.events(day, kinds=(
            "order_rejected", "order_cancel", "recovery", "order_event", "sizing_difference",
            "stop_failed", "stop_update_failed", "flatten_not_working", "orphan_position",
            "entry_skip", "stream", "http_error", "broker_error_body", "trade_closed", "fill",
            "signal", "order_intent", "scan_missed", "data_divergence", "missing_iex_bars"))
        quotes = logs.events(day, kinds=("entry_quote",))
        by_symbol: dict[str, list[dict]] = {}
        for e in quotes:
            if e.get("symbol"):
                by_symbol.setdefault(e["symbol"], []).append(e)
        day_quotes[day] = by_symbol

    trades: list[Trade] = []
    for trip in closed + opened:
        trades.append(_enrich(trip, plans, candidates, sj_candidates, sj_trades, diagnostics, runs,
                              run_for_day, orders_by_key, intent_by_symbol, day_events, day_quotes, notes))
    trades.sort(key=lambda t: (t.day, t.raw["entry_time"] or "", t.symbol))
    counts = {}
    for trade in trades:
        key = (trade.day, trade.symbol)
        counts[key] = counts.get(key, 0) + 1
        trade.sequence = counts[key]
    for trade in trades:
        if counts[(trade.day, trade.symbol)] > 1:
            trade.raw['initial_risk'] = trade.raw['r'] = None
            trade.r_source = 'unavailable'
            trade.loss_class = 'unknown'
            trade.record_gaps.append('multiple trips share symbol/day plan and process context; attribution unresolved')

    equity_points = [{"t": r["started_at"], "equity": _dec(r.get("equity")), "run_id": r["run_id"],
                      "git_commit": r.get("git_commit"), "config_version": r.get("config_version")}
                     for r in tlog.runs() if r.get("equity")]
    starting_equity = equity_points[0]["equity"] if equity_points else None
    if not any(p["equity"] is not None for p in equity_points):
        notes.append("no account equity snapshot is recorded in runs; the equity curve is derived from "
                     "realized P&L only and no broker equity series is claimed")
    if not plans:
        notes.append("no trade plans are recorded; planned entry/stop and frozen R fall back to "
                     "STOP_ATR_FRACTION x ATR, or are unavailable where ATR is missing")
    if not candidates:
        notes.append("no candidate rows are recorded; the funnel shows unknown for every gate")
    return TradeSet([t for t in trades if t.raw["open_qty"] == ZERO],
                    [t for t in trades if t.raw["open_qty"] != ZERO],
                    notes, starting_equity, equity_points, dropped)


def _enrich(trip, plans, candidates, sj_candidates, sj_trades, diagnostics, runs, run_for_day,
            orders_by_key, intent_by_symbol, day_events, day_quotes, notes) -> Trade:
    day, symbol = trip["day"], trip["symbol"]
    t = Trade(raw=trip, symbol=symbol, day=day)
    plan = plans.get((day, symbol))
    cand = candidates.get((day, symbol))
    row = sj_trades.get((day, symbol))
    orders = orders_by_key.get((day, symbol), [])
    events = day_events.get(day, [])
    symbol_events = [e for e in events if e.get("symbol") == symbol]
    quotes = day_quotes.get(day, {}).get(symbol, [])

    # -- planning / features ---------------------------------------------------
    if plan:
        t.planned_entry = _dec(plan.get("entry_trigger"))
        t.planned_stop = _dec(plan.get("planned_stop"))
        t.planned_qty = _dec(plan.get("planned_qty"))
        one_r = _dec(plan.get("one_r_per_share"))
        t.one_r_per_share = one_r
        t.r_source = "plan" if one_r else "unavailable"
        t.sizing = plan.get("sizing") if isinstance(plan.get("sizing"), dict) else None
        t.plan_source = "trade_plan"
    if row:
        t.planned_entry = t.planned_entry if t.planned_entry is not None else _dec(row.get("entry_price"))
        t.planned_stop = t.planned_stop if t.planned_stop is not None else _dec(row.get("stop_price"))
        t.final_stop = _dec(row.get("stop_price"))
        t.planned_qty = t.planned_qty if t.planned_qty is not None else _dec(row.get("planned_qty"))
        t.state = row.get("state")
        t.closed_flag = bool(row.get("closed"))
        t.session_day = row.get("day")
        if t.plan_source == "unavailable":
            t.plan_source = "engine_trade_row"
        if t.one_r_per_share is None:
            atr = _dec(row.get("atr"))
            if atr and atr > 0:
                t.one_r_per_share = C.STOP_ATR_FRACTION * atr
                t.r_source = "atr_constant"
                if trip["r"] is not None:
                    trip["r"] = trip["gross_pnl"] / (t.one_r_per_share * trip["qty"]) \
                        if (trip["gross_pnl"] is not None and trip["qty"]) else None
    if not row:
        t.record_gaps.append("no_fills" if not trip["entry_fills"] else "incomplete_round_trip")
    # The append-only trading journal is the richer record, but it only starts on
    # 2026-10-12. The engine's own candidate table carries the same feature fields and
    # covers the earlier sessions, so it is the documented fallback, never a fabrication.
    features = cand or sj_candidates.get((day, symbol))
    if features:
        t.relvol = _dec(features.get("relvol"))
        t.atr = _dec(features.get("atr"))
        t.adv = _dec(features.get("adv"))
        t.opening = {k: features.get(k) for k in ("open", "high", "low", "close", "volume", "direction")}
    else:
        t.record_gaps.append("no_candidate")
    if plan is None and row is None:
        t.record_gaps.append("no_plan")

    # -- strategy version ------------------------------------------------------
    run_id = run_for_day.get(day)
    if run_id is None:
        run_id = next((r["run_id"] for r in sorted(runs.values(), key=lambda r: r["started_at"])
                       if (r.get("started_at") or "")[:10] <= day), None)
    if run_id and run_id in runs:
        t.run_id = run_id
        t.strategy_version = runs[run_id].get("config_version")
        t.git_commit = runs[run_id].get("git_commit")

    # -- exit reason, slippage -------------------------------------------------
    t.exit_reason, t.exit_reason_dominant = _exit_reason(trip["exit_roles"])
    entry = trip["entry_vwap"]
    if entry is not None and t.planned_entry is not None:
        t.entry_slippage = trip["direction"] * (entry - t.planned_entry)
    if t.exit_reason == "stop" and trip["exit_vwap"] is not None and t.final_stop is not None:
        t.exit_slippage = trip["direction"] * (t.final_stop - trip["exit_vwap"])
    exit_reasons = {EXIT_REASON.get(r, r) for r in trip["exit_roles"]}
    has_stop_exit = "stop" in exit_reasons
    flattening = exit_reasons & {"flatten", "post_close"}
    if has_stop_exit and flattening:
        t.execution_events.append("stop_filled_then_flatten")
    if flattening and not has_stop_exit:
        t.execution_defects.append("flatten_without_stop")
    if "post_close" in exit_reasons:
        t.execution_defects.append("after_close_flatten")
    if "eod" in exit_reasons:
        t.execution_events.append("eod_exit")
    if trip["carry"]:
        t.execution_events.append("carry_over")

    # -- execution flags -------------------------------------------------------
    if len(trip["entry_fills"]) > 1:
        t.execution_events.append("entry_partial_fill")
    if len(trip["exit_fills"]) > 1:
        t.execution_events.append("exit_partial_fill")
    if trip["sign_flip"]:
        t.execution_defects.append("sign_flip")
    if bar_diverged(diagnostics.get((day, "divergence")), symbol):
        t.data_flags.append("bar_diverged")
    stops = [o for o in orders if o.get("role") == "stop"]
    if any(o.get("replaces") or o.get("replaced_by") for o in stops) or len(stops) > 1:
        t.execution_events.append("stop_replaced")
    if any(o.get("status") == "rejected" for o in stops):
        t.execution_defects.append("stop_rejected")
    entries = [o for o in orders if o.get("role") == "entry"]
    entry_qty = sum((_dec(o.get("filled_qty") or 0) or ZERO) for o in entries)
    protected = bool(row and (_dec(row.get("protected_qty") or 0) or ZERO) > 0)
    if trip["entry_fills"] and not stops and not protected:
        t.execution_defects.append("entry_unprotected")
    elif stops and entry_qty:
        # A protective stop that covers less than the filled position leaves a window
        # in which the extra shares are unprotected.
        covered = max((_dec(o.get("qty") or 0) or ZERO) for o in stops)
        if covered < entry_qty:
            t.execution_defects.append("stop_coverage_short")
    for o in entries:
        if o.get("status") == "rejected":
            t.execution_defects.append("entry_rejected_unfilled" if not _dec(o.get("filled_qty") or 0)
                                       else "entry_rejected_partial")
            t.broker_flags.append("order_rejected")
    if row and row.get("flatten_attempts") and row["flatten_attempts"] > 1:
        t.execution_defects.append("exit_late")
    if not row and trip["entry_fills"]:
        t.record_gaps.append("incomplete_round_trip")
    for e in symbol_events:
        kind = e.get("event")
        if kind == "order_rejected":
            t.broker_flags.append("order_rejected")
            msg = (e.get("error") or e.get("msg") or "")
            if e.get("role") == "entry" and ("greater than current price" in msg or "less than current price" in msg):
                t.execution_defects.append("entry_crossed_skip")
        elif kind == "recovery":
            t.execution_events.append("recovery_event")
        elif kind in ("stop_failed", "stop_update_failed"):
            t.execution_defects.append("stop_update_failed")
        elif kind in ("http_error", "broker_error_body"):
            t.broker_flags.append("broker_error" if kind == "broker_error_body" else "http_error")
        elif kind == "stream" and "reconnect" in str(e.get("msg", "")).lower():
            t.broker_flags.append("stream_reconnect")
    if any(i.get("state") != "TERMINAL" for i in intent_by_symbol.get(symbol, [])):
        t.execution_defects.append("unresolved_intent")
    if [q for q in quotes if q.get("verdict") == "stale_quote"]:
        t.data_flags.append("quote_stale")
    if [q for q in quotes if q.get("verdict") == "skip_spread_too_wide"]:
        t.data_flags.append("spread_too_wide")
    if trip["exit_qty"] and trip["qty"] and trip["exit_qty"] != trip["qty"] and trip["open_qty"] == 0:
        t.record_gaps.append("incomplete_round_trip")

    # -- loss classification ---------------------------------------------------
    t.execution_defects = sorted(set(t.execution_defects))
    t.execution_events = sorted(set(t.execution_events))
    t.broker_flags = sorted(set(t.broker_flags))
    t.data_flags = sorted(set(t.data_flags))
    t.record_gaps = sorted(set(t.record_gaps))
    t.loss_class, t.loss_reasons = classify(t)
    return t


def classify(t: Trade) -> tuple[str, list[str]]:
    """Which kind of outcome is this: strategy, execution, data, broker, mixed or unknown.

    Deterministic, evidence-based and deliberately narrow: only a real defect, broker
    failure or unusable input moves a loss out of ``normal_strategy``. Normal mechanics
    (partial fills, stop replacement, EOD exits) and incomplete records never do, because
    they describe how the trade ran or how well it was recorded, not whose fault it was.
    """
    causes: list[str] = []
    reasons: list[str] = []
    if t.execution_defects:
        causes.append("execution_defect")
        reasons.append("execution: " + ", ".join(t.execution_defects))
    if t.broker_flags:
        causes.append("broker_issue")
        reasons.append("broker: " + ", ".join(t.broker_flags))
    if t.data_flags:
        causes.append("data_issue")
        reasons.append("data: " + ", ".join(t.data_flags))
    if len(causes) > 1:
        return "mixed", reasons
    if len(causes) == 1:
        return causes[0], reasons
    if t.raw["realized_pnl"] is None:
        return "unknown", ["no realized P&L: the position was never closed inside the record"]
    return "normal_strategy", ["no execution, data or broker deviation recorded"
                               + (f" (record gaps: {', '.join(t.record_gaps)})" if t.record_gaps else "")]
