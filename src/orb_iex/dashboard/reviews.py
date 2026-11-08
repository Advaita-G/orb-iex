"""Calendar and reviews.

The calendar answers one question per day: what state was this day in, and what
happened? Five states, never blurred together:

``traded``               the session ran and at least one position filled
``healthy_no_trades``    the session ran and the rules excluded every candidate
``not_run``              the bot ran but the scan window was missed
``unavailable``          no session record at all (a holiday, or the bot did not run)
``market_closed``        a weekend

Weekends are known offline. Whether a missing weekday was a market holiday or a
bot that never started cannot be settled without Alpaca's calendar, so those days
are reported as ``unavailable`` and said so, rather than guessed either way.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Sequence

from . import funnel as funnel_mod
from . import metrics
from .trades import ZERO, Trade, q


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = (int(x) for x in month.split("-")[:2])
    last = monthrange(year, mon)[1]
    return date(year, mon, 1), date(year, mon, last)


def week_bounds(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def day_state(sources, day: str, trades: Sequence[Trade]) -> dict[str, Any]:
    """One calendar cell: state plus the numbers behind it."""
    d = date.fromisoformat(day)
    rows = [t for t in trades if t.day == day]
    realized = sum((t.realized for t in rows if t.realized is not None), ZERO)
    net = sum((t.net for t in rows if t.net is not None), ZERO)
    rs = [t.r for t in rows if t.r is not None]
    # Trades touched by a deviation, counted once each: the same unit as
    # metrics.perf()["execution_incidents"] and metrics.incidents_by_day(), so one trade
    # with three defects cannot read as three incidents on the calendar.
    incidents = sum(1 for t in rows if t.execution_defects or t.broker_flags or t.data_flags)
    session = sources.state_journal.day(day)
    signals = None
    filled = None
    diagnosis = None
    if session is not None:
        f = funnel_mod.build(sources, day)
        signals = f["summary"]["signals"]
        filled = f["summary"]["filled"]
        diagnosis = f["summary"]["diagnosis"]
    if d.weekday() >= 5:
        state = "market_closed"
        detail = "weekend"
    elif session is None:
        state = "unavailable"
        detail = "no session row: a market holiday, or the bot did not run. Alpaca's calendar is not consulted offline"
    elif session["scan_status"] != "ok":
        state = "not_run"
        detail = f"scan status {session['scan_status']!r}: no entry decision was made"
    elif rows:
        state = "traded"
        detail = f"{len(rows)} position(s) closed"
    elif filled:
        state = "traded"
        detail = f"{filled} filled position(s) still open at the close"
    else:
        state = "healthy_no_trades"
        detail = ("the session ran and every candidate was filtered out"
                  if not signals else f"{signals} signal(s), none filled")
    return {
        "day": day, "weekday": metrics.weekday_bucket(day), "state": state, "detail": detail,
        "trades": len(rows), "realized_pnl": q(realized), "modeled_net_pnl": q(net),
        "net_r": q(sum(rs, ZERO), "0.01") if rs else None,
        "r_available": len(rs),
        "incidents": incidents,
        "signals": signals, "filled": filled,
        "scan_status": session and session["scan_status"],
        "eod_status": session and session["eod_status"],
        "diagnosis": diagnosis,
        "symbols": sorted({t.symbol for t in rows}),
    }


def calendar(sources, month: str, trades: Sequence[Trade]) -> dict[str, Any]:
    first, last = month_bounds(month)
    cells = []
    day = first
    while day <= last:
        iso = day.isoformat()
        cells.append(day_state(sources, iso, trades))
        day += timedelta(days=1)
    traded = [c for c in cells if c["state"] == "traded"]
    nets = [Decimal(c["modeled_net_pnl"]) for c in cells if c["modeled_net_pnl"]]
    return {
        "month": month,
        "cells": cells,
        "totals": {
            "sessions_recorded": sum(1 for c in cells if c["state"] in ("traded", "healthy_no_trades", "not_run")),
            "traded": len(traded),
            "healthy_no_trades": sum(1 for c in cells if c["state"] == "healthy_no_trades"),
            "not_run": sum(1 for c in cells if c["state"] == "not_run"),
            "market_closed": sum(1 for c in cells if c["state"] == "market_closed"),
            "unavailable": sum(1 for c in cells if c["state"] == "unavailable"),
            "trades": sum(c["trades"] for c in cells),
            "realized_pnl": q(sum((Decimal(c["realized_pnl"]) for c in cells if c["realized_pnl"]), ZERO)),
            "modeled_net_pnl": q(sum(nets, ZERO)),
            "incidents": sum(c["incidents"] for c in cells),
        },
        "state_legend": {
            "traded": "the session ran and at least one position filled",
            "healthy_no_trades": "the session ran and the rules excluded every candidate",
            "not_run": "the bot ran but missed the scan window",
            "unavailable": "no session record (holiday, or the bot did not run)",
            "market_closed": "weekend",
        },
    }


def months(sources, trades: Sequence[Trade]) -> list[str]:
    found = {t.day[:7] for t in trades}
    found |= {d["day"][:7] for d in sources.state_journal.days()}
    return sorted(found, reverse=True)


def month_list(sources, trades: Sequence[Trade], *, span: int = 3) -> list[dict[str, Any]]:
    months_ = months(sources, trades) or [date.today().isoformat()[:7]]
    return [calendar(sources, m, trades) for m in months_[:span]]


# ---------------------------------------------------------------------------
# reviews
# ---------------------------------------------------------------------------

def _rows_window(trades: Sequence[Trade], start: str, end: str) -> list[Trade]:
    return [t for t in trades if start <= t.day <= end]


def review(sources, trades: Sequence[Trade], start: str, end: str, label: str,
           incidents_by_day: dict[str, dict[str, int]] | None = None) -> dict[str, Any]:
    rows = _rows_window(trades, start, end)
    perf = metrics.perf(rows, label=label)
    incident_total = sum((incidents_by_day or {}).get(d, {}).get("defects", 0)
                         for d in {t.day for t in rows})
    strategy_rows = [t for t in rows if t.loss_class == "normal_strategy" and t.net is not None]
    defect_rows = [t for t in rows if t.execution_defects and t.realized is not None]
    slippage_rows = [t for t in rows if t.entry_slippage is not None]
    days = sorted({t.day for t in rows})
    return {
        "label": label, "start": start, "end": end,
        "days": days, "trading_days": len(days),
        "metrics": perf,
        "ratios": metrics.ratios(rows, None),
        "equity": metrics.equity_curve(rows, None),
        "funnel": _funnel_rollup(sources, days),
        "best_strategy_trades": _rank(strategy_rows, "net", reverse=True)[:3],
        "worst_strategy_trades": _rank(strategy_rows, "net")[:3],
        "best_execution": [t.to_dict() for t in sorted(
            defect_rows, key=lambda t: (-(t.net or ZERO), t.symbol))[:3]],
        "worst_execution": [t.to_dict() for t in sorted(
            defect_rows, key=lambda t: (t.net or ZERO, t.symbol))[:3]],
        "largest_incident_losses": [t.to_dict() for t in sorted(
            [t for t in rows if (t.execution_defects or t.broker_flags or t.data_flags)
             and t.realized is not None], key=lambda t: t.realized)[:5]],
        "largest_slippage": [{"trade_id": t.trade_id, "symbol": t.symbol, "day": t.day,
                              "entry_slippage_per_share": q(t.entry_slippage, "0.0001"),
                              "entry_slippage_dollars": q(t.entry_slippage * t.raw["qty"],
                                                          "0.01") if t.entry_slippage is not None else None}
                             for t in sorted(slippage_rows, key=lambda t: t.entry_slippage, reverse=True)[:5]],
        "incidents_by_day": {d: (incidents_by_day or {}).get(d, {}) for d in days},
        "defect_trades": incident_total,
        "notes": _notes(rows, perf),
        "day_summaries": [s for s in sources.trade_journal.day_summaries() if start <= s["day"] <= end],
        "engine_summaries": [{"day": d, "summary": sources.state_journal.summary(d)}
                             for d in days if sources.state_journal.summary(d)],
    }


def _funnel_rollup(sources, days: Sequence[str]) -> dict[str, Any]:
    signals = filled = closed = 0
    states: dict[str, int] = {}
    per_day = []
    for day in days:
        f = funnel_mod.build(sources, day)
        summary = f["summary"]
        signals += summary["signals"]
        filled += summary["filled"]
        closed += summary["closed"]
        state = summary["diagnosis"]["state"]
        states[state] = states.get(state, 0) + 1
        per_day.append({"day": day, "attempted": summary["attempted"], "signals": summary["signals"],
                        "filled": summary["filled"], "closed": summary["closed"], "state": state,
                        "headline": summary["diagnosis"]["headline"]})
    return {"days": len(days), "signals": signals, "filled": filled, "closed": closed,
            "states": states, "per_day": per_day}


def _rank(rows: Sequence[Trade], key: str, reverse: bool = False) -> list[dict[str, Any]]:
    """Ascending by default (worst first); ``reverse=True`` puts the best first."""
    ordered = sorted(rows, key=lambda t: (t.net if key == "net" else t.r) or ZERO, reverse=reverse)
    return [t.to_dict() for t in ordered[:3]]


def _notes(rows: Sequence[Trade], perf: dict[str, Any]) -> list[str]:
    notes = []
    if len(rows) < metrics.MIN_TRADES_FOR_STATS:
        notes.append(f"only {len(rows)} trade(s) in this window; every ratio here is a sample of one-ish")
    if perf["r_unavailable"]:
        notes.append(f"{perf['r_unavailable']} trade(s) have no frozen initial risk, so no R")
    if any(t.record_gaps for t in rows):
        notes.append("some trades have record gaps: " + ", ".join(
            sorted({g for t in rows for g in t.record_gaps})))
    if not notes:
        notes.append("sample size is shown next to every statistic; treat small windows as anecdote")
    return notes


def build(sources, trades: Sequence[Trade], *, day: str | None = None,
          incidents_by_day: dict[str, dict[str, int]] | None = None) -> dict[str, Any]:
    days = sorted({t.day for t in trades}) or [d["day"] for d in sources.state_journal.days()]
    latest = day or (days[-1] if days else date.today().isoformat())
    d = date.fromisoformat(latest)
    wk_start, wk_end = week_bounds(d)
    return {
        "latest_day": latest,
        "daily": review(sources, trades, latest, latest, latest, incidents_by_day),
        "weekly": review(sources, trades, wk_start.isoformat(), wk_end.isoformat(),
                         f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}", incidents_by_day),
        "monthly": review(sources, trades, latest[:7] + "-01",
                          month_bounds(latest[:7])[1].isoformat(), latest[:7], incidents_by_day),
        "all_time": review(sources, trades, "0000-01-01", "9999-12-31", "all recorded sessions",
                           incidents_by_day),
        "calendar_months": months(sources, trades),
        "calendar": calendar(sources, latest[:7], trades),
    }
