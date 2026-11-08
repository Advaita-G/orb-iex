"""PAPER versus backtest / research.

Two rules govern this page:

1. A PAPER result is never presented as proof of live tradability. Alpaca paper
   fills the bot's own order against a simulated book: it validates plumbing, not
   capacity, queue position or borrow.
2. A comparison is only drawn where the two sides are actually comparable. The
   research artifacts are aggregate (model x horizon), so per-day alignment,
   per-candidate matching and fill/no-fill matching are reported as unavailable
   with the exact artifact the bot would need to produce, rather than mocked up.

What IS compared with real data: cost model, planned vs actual entry, expected vs
observed slippage, expected vs actual R, expected vs modeled net P&L, and the
sizing differences the engine logged against its own unrounded reference.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Sequence

from . import metrics
from .trades import ZERO, Trade, q

BACKTEST_NOTE = ("backtest metrics come from runtime/research/orb_results.json as written by the "
                 "research run; they are aggregate per cost model and horizon")


def _metric_block(source: dict) -> dict[str, Any]:
    keys = ("trades", "sessions", "years", "total_return", "cagr", "sharpe", "sortino", "max_drawdown",
            "calmar", "ann_vol", "win_rate", "avg_win_r", "avg_loss_r", "payoff", "profit_factor",
            "expectancy_r", "expectancy_usd", "avg_gross_leverage", "turnover_per_year", "exposure_days",
            "end_equity")
    out = {}
    for k in keys:
        v = source.get(k)
        if isinstance(v, float):
            out[k] = str(round(v, 6))
        else:
            out[k] = v
    return out


def backtest_models(research: dict | None) -> list[dict[str, Any]]:
    if not research or "baseline" not in research:
        return []
    rows = []
    for model, horizons in research["baseline"].items():
        if not isinstance(horizons, dict):
            continue
        for horizon, body in horizons.items():
            if not isinstance(body, dict):
                continue
            rows.append({"model": model, "horizon": horizon, **_metric_block(body)})
    return rows


def variants(research: dict | None) -> list[dict[str, Any]]:
    """The preregistered variants (E1_or1 ... E4_atrpct) as research context."""
    if not research or "matrix" not in research:
        return []
    matrix = research["matrix"]
    if isinstance(matrix, dict):
        return [{"variant": name, "body": body} for name, body in matrix.items()]
    return []


def segment_notes(research: dict | None) -> dict[str, Any]:
    if not research or "segments" not in research:
        return {"available": False, "reason": "no segment block in the research artifact"}
    return {"available": True, "segments": research["segments"], "note":
            "backtest segments by direction, ETF-likeness and regime; the live journal records no regime, "
            "so only direction and volatility buckets can be compared like for like"}


def build(sources, trades: Sequence[Trade], *, base_equity: Decimal | None = None,
          window_label: str = "all", recorded_points: Sequence[dict] | None = None) -> dict[str, Any]:
    research = sources.research.orb()
    tlog = sources.trade_journal
    runs = tlog.runs()
    paper = metrics.perf(trades, label="PAPER live")
    paper_ratios = metrics.ratios(trades, base_equity, key="net")
    events = [e for e in tlog.events() if e.get("kind") == "sizing_difference"]
    config = sources.config
    modeled_per_share = _dec(config.get("slippage_per_share"))
    observed_slip = [t.entry_slippage for t in trades if t.entry_slippage is not None]
    mean_observed = (sum(observed_slip, ZERO) / len(observed_slip)) if observed_slip else None
    models = backtest_models(research)
    pessimistic = next((m for m in models if m["model"] == "pessimistic/paper" and m["horizon"] == "ALL"), None)
    calibrated = next((m for m in models if m["model"] == "calibrated/paper" and m["horizon"] == "ALL"), None)
    reference = calibrated or pessimistic
    expected_r = _dec(reference.get("expectancy_r")) if reference else None
    paper_r = _dec(paper["mean_r"])
    expected_pnl = (expected_r * sum((t.raw["initial_risk"] for t in trades if t.raw["initial_risk"]),
                                     ZERO)) if expected_r else None
    realized = sum((t.net for t in trades if t.net is not None), ZERO)

    return {
        "banner": ("PAPER fills prove the plumbing works. They do not prove live tradability: paper has no "
                   "queue position, no partial-fill market impact, no borrow constraint and no SIP-quality "
                   "opening bar (ORB-IEX reads the free IEX feed)."),
        "window": window_label,
        "paper": {
            **paper,
            "ratios": paper_ratios,
            "first_day": min((t.day for t in trades), default=None),
            "last_day": max((t.day for t in trades), default=None),
        },
        "strategy_versions": [
            {"git_commit": r.get("git_commit"), "config_version": r.get("config_version"),
             "bot_version": r.get("bot_version"), "first_run": r.get("started_at"),
             "mode": r.get("mode"), "equity": r.get("equity")}
            for r in _dedupe_versions(runs)],
        "backtest": {"note": BACKTEST_NOTE, "source": (research or {}).get("_path"),
                     "as_of": (research or {}).get("_mtime"), "models": models,
                     "reference_model": "calibrated/paper ALL" if calibrated else
                                        "pessimistic/paper ALL" if pessimistic else None,
                     "variants": variants(research), "segments": segment_notes(research)},
        "alignment": {
            "matching_dates": {
                "available": False,
                "paper_days": sorted({t.day for t in trades}),
                "reason": "the research artifact is aggregate (model x horizon); it stores no per-session "
                          "returns, so no day-by-day alignment is possible",
                "data_needed": "a per-session return series in runtime/research/orb_results.json "
                               "(sessions[] with date + return)",
            },
            "matching_strategy_versions": {
                "available": bool(runs),
                "paper": sorted({r.get("config_version") for r in runs if r.get("config_version")}),
                "backtest": (research or {}).get("config_version"),
                "reason": None if runs else "no runs recorded in the trading journal",
            },
            "aligned_equity_curve": {
                "available": False,
                "paper": metrics.equity_curve(trades, base_equity, recorded_points),
                "reason": "the backtest artifact holds no equity time series to overlay",
                "data_needed": "an equity/drawdown series per session from the research run",
            },
            "matched_candidates": {
                "available": False,
                "reason": "the research run does not export its per-session candidate list",
                "data_needed": "the backtest candidate table (day, symbol, rank, relvol, atr, plan, filled)",
            },
            "unmatched_candidates": {"available": False, "reason": "same as matched candidates"},
            "signal_mismatch": {
                "available": False,
                "reason": "signals cannot be matched without the backtest candidate table",
                "data_needed": "backtest signals per session",
            },
            "sizing_mismatch": {
                "available": bool(events),
                "events": len(events),
                "kind": "engine vs its own unrounded reference price",
                "detail": [
                    {"ts": e.get("event_time"), "symbol": e.get("symbol"),
                     "message": (e.get("detail") or {}) if isinstance(e.get("detail"), dict) else None}
                    for e in events[:50]],
                "reason": None if events else "no sizing_difference events were journaled",
                "note": "this is ORB-IEX against itself (sub-penny rounding), not against the backtest",
            },
            "fill_no_fill_mismatch": {
                "available": False,
                "reason": "the research run does not export per-candidate fills",
                "data_needed": "backtest fills per candidate",
            },
        },
        "planned_vs_actual": _planned_vs_actual(trades),
        "slippage": {
            "expected_per_share_model": None if modeled_per_share is None else str(modeled_per_share),
            "observed_mean_per_share": q(mean_observed, "0.0001") if mean_observed is not None else None,
            "observed_per_share_dist": metrics.dist([float(x) for x in observed_slip]) if observed_slip else None,
            "observed_in_R": metrics.dist([float(t.entry_slippage / t.one_r_per_share) for t in trades
                                           if t.entry_slippage is not None and t.one_r_per_share]) or None,
            "note": ("the modeled per-share slippage is the cost model the research uses; observed slippage is "
                     "measured against the planned trigger inside the real fill prices, so the two are separate "
                     "quantities and are never added together"),
            "unavailable": None if observed_slip else "no trade has a planned trigger recorded, so observed "
                                                      "slippage cannot be measured",
        },
        "expectancy": {
            "backtest_expectancy_r": None if expected_r is None else str(expected_r),
            "backtest_reference": reference and f"{reference['model']} / {reference['horizon']}",
            "paper_mean_r": paper["mean_r"],
            "paper_expectancy_r": paper["expectancy_r"],
            "paper_trades": paper["trades"],
            "difference_r": None if (expected_r is None or paper_r is None) else q(paper_r - expected_r, "0.01"),
            "valid": bool(expected_r is not None and paper["valid"]),
            "reason": None if expected_r is not None else "the research artifact has no expectancy_r",
        },
        "costs": {
            "paper_modeled_cost_per_trade": paper["cost_per_trade"],
            "paper_modeled_cost_total": paper["modeled_cost"],
            "backtest_cost_note": ("the research models costs per cost-model arm (pessimistic / calibrated / "
                                   "optimistic); compare its modeled net P&L against the PAPER modeled net, "
                                   "never against the raw realized P&L"),
            "stress": metrics.stress(trades),
        },
        "residual": {
            "realized_vs_backtest": {
                "paper_modeled_net_pnl": q(realized),
                "backtest_expected_pnl_at_paper_risk": q(expected_pnl) if expected_pnl is not None else None,
                "unexplained": q(realized - expected_pnl) if expected_pnl is not None else None,
                "reason": ("not decomposable from these artifacts: the backtest has no per-session output, so "
                           "the residual cannot be split into signal, sizing, fill and cost components"
                           if expected_pnl is not None else "no backtest expectancy available"),
            },
            "paper_trades_vs_backtest_trades": {
                "paper": paper["trades"],
                "backtest": (reference or {}).get("trades"),
                "reason": "the backtest spans years over a full universe; the PAPER sample is not comparable in size",
            },
        },
        "unavailable_summary": [
            "per-session backtest returns", "backtest candidate table", "backtest fills",
            "aligned equity curve", "market/volatility regime per live session",
        ],
    }


def _planned_vs_actual(trades: Sequence[Trade]) -> dict[str, Any]:
    rows = []
    for t in trades:
        d = t.to_dict()
        rows.append({"trade_id": t.trade_id, "symbol": t.symbol, "day": t.day,
                     "planned_entry": d["planned_entry"], "entry_vwap": d["entry_vwap"],
                     "entry_slippage_per_share": d["entry_slippage_per_share"],
                     "entry_slippage_dollars": d["entry_slippage_dollars"],
                     "planned_qty": d["planned_qty"], "filled_qty": d["qty"],
                     "fill_ratio": q(t.raw["qty"] / t.planned_qty, "0.0001")
                     if t.planned_qty else None})
    slippages = [float(t.entry_slippage) for t in trades if t.entry_slippage is not None]
    return {"rows": rows, "n": len(rows),
            "slippage_dist": metrics.dist(slippages) if slippages else None,
            "adverse_share": q(Decimal(sum(1 for s in slippages if s > 0)) / len(slippages), "0.0001")
            if slippages else None,
            "note": "slippage is signed in the trade's favour: positive = worse than the planned trigger"}


def _dedupe_versions(runs: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for r in runs:
        key = (r.get("git_commit"), r.get("config_version"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None
