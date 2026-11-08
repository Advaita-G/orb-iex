"""Execution & Safety: is the bot safe, connected, reconciled and behaving?

Everything on this page is evidence, ranked by how much it should worry you:

1. Live safety state — heartbeat, entry gate, kill switch, breakers, unresolved
   intents, flat gate, disk, journal integrity.
2. Broker vs local — positions and open orders from the last read-only broker
   snapshot against what the journal believes it owns, with the age of each.
3. Protection coverage — every filled position and the stop that covers it.
4. Defects and incidents — rejections, stop failures, flattens, reconnects,
   ambiguous mutations, duplicate prevention, stale entries, overnight positions.
5. Quality distributions — quote age, spread, latency, slippage, partial fills.

Nothing here is inferred as healthy. A missing source reports ``unavailable`` with
the exact file or table the bot would have to write.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

from .. import constants as C
from ..ops import HEARTBEAT_MAX_AGE_SECONDS, heartbeat_state
from .metrics import dist
from .trades import D, ZERO, Trade, q

INCIDENT_KINDS = {
    "order_rejected": "Order rejected by the broker",
    "order_cancel": "Order cancelled",
    "stop_failed": "Protective stop failed",
    "stop_update_failed": "Stop change failed",
    "flatten_not_working": "Flatten did not work",
    "orphan_position": "Overnight / orphan position",
    "orphan_stop": "Orphan stop order",
    "overnight_positions": "Overnight exposure",
    "open_after_close": "Position still open after the close",
    "fatal_flatten": "Fatal-error flatten",
    "cancel_failed": "Cancel failed",
    "poll_failed": "Order poll failed",
    "http_error": "HTTP error talking to Alpaca",
    "broker_error_body": "Broker error response",
    "scan_missed": "Scan window missed",
    "data_divergence": "09:35 vs 09:37 opening-bar divergence",
    "missing_iex_bars": "Missing IEX opening bars",
    "host_suspended": "Host suspended (laptop slept)",
    "shutdown_incomplete": "Shutdown did not complete",
    "stale_lock": "Stale instance lock recovered",
    "auto_control_failure": "Automatic safety breaker set",
    "sizing_difference": "Sizing differed from the unrounded reference",
}
QUOTE_EVENT = "entry_quote"
STREAM_EVENT = "stream"


# ---------------------------------------------------------------------------
# 1. live safety state
# ---------------------------------------------------------------------------

def safety_state(sources) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    heartbeat = sources.health.heartbeat()
    paper_auto = sources.health.paper_auto() or {}
    shutdown = sources.health.shutdown()
    startup_failure = sources.health.startup_failure()
    state_journal = sources.state_journal
    breakers = state_journal.breakers()
    intents = state_journal.intents()
    unresolved = [i for i in intents if i.get("state") != "TERMINAL"]
    ambiguous = [i for i in intents if i.get("state") == "AMBIGUOUS"]
    risk = state_journal.risk()
    unprotected = [r for r in risk if r.get("unprotected_since")]

    hb_state, hb_detail = heartbeat_state(heartbeat, now) if heartbeat else ("missing", "no heartbeat file")
    breaker_rows = [{"kind": b["kind"], "symbol": b["symbol"], "detail": b["detail"], "ts": b["ts"],
                     "class": "manual" if b["kind"] not in
                     ("auto_clock_skew", "auto_safety_failure", "auto_control_failure", "auto_broker_failure",
                      "auto_calendar_failure", "auto_market_closed", "auto_data_failure") else "automatic"}
                    for b in breakers]
    checks = [
        _check("Process heartbeat", hb_state == "healthy",
               f"{hb_state}: {hb_detail}", "health/heartbeat.json"),
        _check("PAPER mode", True,
               f"mode {heartbeat.get('mode') if heartbeat else 'unknown'}; the trading host is the constant "
               f"{C.PAPER_TRADING_HOST} and no setting can change it", "constants.PAPER_TRADING_HOST"),
        _check("External kill switch", not bool(paper_auto.get("external_kill_latched")),
               "latched" if paper_auto.get("external_kill_latched") else "not latched",
               "health/paper-auto.json"),
        _check("Entry gate", bool(paper_auto.get("entry_gate_open")),
               "open" if paper_auto.get("entry_gate_open") else "closed / not yet opened",
               "health/paper-auto.json"),
        _check("Persistent breakers", not breaker_rows,
               f"{len(breaker_rows)} breaker(s): " + ", ".join(sorted({b['kind'] for b in breaker_rows}))
               if breaker_rows else "none latched", "journal.sqlite:breakers"),
        _check("Unresolved intents", not unresolved,
               f"{len(unresolved)} unresolved" + (f", {len(ambiguous)} ambiguous" if ambiguous else "")
               if unresolved else "none", "journal.sqlite:intents"),
        _check("Unprotected positions", not unprotected,
               f"{len(unprotected)} position(s): " + ", ".join(r["symbol"] for r in unprotected)
               if unprotected else "none", "journal.sqlite:risk"),
    ]
    integrity = state_journal.integrity()
    checks.append(_check("State journal integrity", integrity["state"] == "ok",
                         integrity.get("detail") or integrity["state"], str(state_journal.path)))
    fallback = state_journal.emergency_fallback()
    checks.append(_check("Emergency write-ahead file", not fallback["present"],
                         "present and awaiting reconciliation" if fallback["present"] else "absent",
                         fallback["path"]))
    release = paper_auto.get("status")
    return {
        "as_of": now.isoformat(),
        "heartbeat_state": hb_state,
        "heartbeat": heartbeat,
        "heartbeat_max_age_seconds": HEARTBEAT_MAX_AGE_SECONDS,
        "runtime_report": {k: v for k, v in paper_auto.items() if not k.startswith("_")},
        "runtime_report_age_seconds": _age_seconds(paper_auto.get("updated_at"), now),
        "shutdown": {k: v for k, v in (shutdown or {}).items() if not k.startswith("_")} or None,
        "startup_failure": {k: v for k, v in (startup_failure or {}).items() if not k.startswith("_")} or None,
        "breakers": breaker_rows,
        "breaker_history": paper_auto.get("breaker_history") or [],
        "breaker_resolutions": paper_auto.get("breaker_resolutions") or [],
        "unresolved_intents": unresolved,
        "ambiguous_intents": ambiguous,
        "unprotected_risk_rows": unprotected,
        "release_status": release,
        "checks": checks,
        "blocking": [c for c in checks if not c["ok"]],
    }


def _check(name: str, ok: bool, detail: str, source: str) -> dict[str, Any]:
    return {"check": name, "ok": bool(ok), "detail": detail, "source": source}


def _age_seconds(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    try:
        return round((now - datetime.fromisoformat(stamp)).total_seconds(), 1)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 2. broker vs local
# ---------------------------------------------------------------------------

def broker_vs_local(sources) -> dict[str, Any]:
    """The broker's own view (the last read-only snapshot) against the journal's."""
    snapshots = sources.broker_snapshots.list()
    latest = snapshots[-1] if snapshots else None
    journal_owned = sources.state_journal.owned_qty()
    paper_auto = sources.health.paper_auto() or {}
    runtime_positions = paper_auto.get("final_positions")
    runtime_orders = paper_auto.get("final_open_orders")
    broker_positions = None
    source = None
    observed_at = None
    if runtime_positions is not None:
        broker_positions = {k: v for k, v in runtime_positions.items() if D(str(v)) != 0} \
            if all(_is_number(v) for v in runtime_positions.values()) else runtime_positions
        source = "health/paper-auto.json (end-of-session broker snapshot)"
        observed_at = paper_auto.get("updated_at")
    if latest:
        snap_positions = latest.get("positions")
        if isinstance(snap_positions, dict):
            # Prefer the newer of the two, but say which one was used.
            if observed_at is None or (latest.get("observed_at") or "") > observed_at:
                broker_positions = {k: v for k, v in snap_positions.items() if str(v) not in ("0", "0.0")}
                source = f"research/{latest['name']}"
                observed_at = latest.get("observed_at")
    open_orders = runtime_orders if runtime_orders is not None else None
    nonterminal = [o for o in sources.state_journal.orders() if o.get("status") not in
                   ("filled", "canceled", "cancelled", "rejected", "expired", "done_for_day")]
    items: list[dict[str, Any]] = []
    for symbol, qty in (broker_positions or {}).items():
        local = journal_owned.get(symbol)
        items.append({"symbol": symbol, "broker_qty": str(qty), "journal_qty": local,
                      "match": local is not None and D(str(local)) == D(str(qty)),
                      "note": "no journal fill ever recorded for this symbol" if local is None
                      else None})
    for symbol, qty in journal_owned.items():
        if broker_positions is not None and symbol not in broker_positions:
            items.append({"symbol": symbol, "broker_qty": "0", "journal_qty": qty, "match": False,
                          "note": "the journal believes the bot holds this; the broker snapshot does not"})
    return {
        "broker_positions": broker_positions,
        "broker_positions_source": source,
        "broker_snapshot_at": observed_at,
        "broker_snapshot_age_seconds": _age_seconds(observed_at, datetime.now(timezone.utc)),
        "journal_owned_qty": journal_owned,
        "open_orders_from_report": open_orders,
        "journal_nonterminal_orders": [
            {"symbol": o["symbol"], "client_order_id": o["client_order_id"], "role": o["role"],
             "status": o["status"], "qty": o["qty"], "filled_qty": o["filled_qty"], "day": o["day"]}
            for o in nonterminal],
        "differences": items,
        "reconciled": not items,
        "flat_gate": paper_auto.get("flat_gate"),
        "exposure_flat": paper_auto.get("exposure_flat"),
        "snapshots_available": [{"name": s["name"], "observed_at": s["observed_at"],
                                 "positions": s.get("positions"), "flat_gate": s.get("flat_gate")}
                                for s in snapshots],
        "note": ("the broker is only known through read-only snapshots the bot saved; the dashboard never "
                 "calls the broker. Age matters: an old snapshot is not evidence about now"),
    }


def _is_number(v) -> bool:
    try:
        D(str(v))
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 3. protection coverage
# ---------------------------------------------------------------------------

def protection(sources, trades: Sequence[Trade]) -> dict[str, Any]:
    state = sources.state_journal
    rows = []
    for t in trades:
        orders = state.orders(t.day, t.symbol)
        stops = [o for o in orders if o.get("role") == "stop"]
        protected = D(str(next((o.get("protected_qty") for o in state.trades(t.day)
                                if o["symbol"] == t.symbol), 0) or 0))
        covered = max((D(str(o.get("qty") or 0)) for o in stops), default=ZERO)
        rows.append({
            "trade_id": t.trade_id, "symbol": t.symbol, "day": t.day,
            "filled_qty": str(t.raw["qty"]),
            "stop_orders": [{"client_order_id": o.get("client_order_id"), "qty": o.get("qty"),
                             "stop_price": o.get("stop_price"), "status": o.get("status"),
                             "filled_qty": o.get("filled_qty")} for o in stops],
            "stop_covered_qty": str(covered),
            "protected_qty": str(protected),
            "coverage": "full" if covered >= t.raw["qty"] else ("none" if not stops else "partial"),
            "defects": [f for f in t.execution_defects
                        if f in ("entry_unprotected", "stop_coverage_short", "stop_rejected")],
        })
    uncovered = [r for r in rows if r["coverage"] != "full"]
    return {"positions": rows, "full_coverage": len(rows) - len(uncovered), "not_full": len(uncovered),
            "defect_count": sum(1 for r in rows if r["defects"])}


# ---------------------------------------------------------------------------
# 4. incidents
# ---------------------------------------------------------------------------

def incidents(sources, days: Iterable[str] | None = None) -> dict[str, Any]:
    logs = sources.logs
    days = list(days) if days is not None else logs.available_days()
    timeline: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for day in days:
        for event in logs.events(day, kinds=tuple(INCIDENT_KINDS)):
            kind = event.get("event")
            counts[kind] = counts.get(kind, 0) + 1
            timeline.append({
                "ts": event.get("ts"), "day": day, "kind": kind,
                "label": INCIDENT_KINDS.get(kind, kind), "level": event.get("level"),
                "symbol": event.get("symbol"), "role": event.get("role"),
                "message": event.get("msg"),
            })
    state = sources.state_journal
    rejections = [{"client_order_id": o["client_order_id"], "symbol": o["symbol"], "role": o["role"],
                   "day": o["day"], "status": o["status"], "qty": o["qty"]}
                  for o in state.orders() if o.get("status") == "rejected"]
    stop_rejections = [r for r in rejections if r["role"] == "stop"]
    return {
        "timeline": sorted(timeline, key=lambda e: e["ts"] or ""),
        "counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "days_covered": days,
        "rejected_orders": rejections,
        "stop_rejections": stop_rejections,
        "note": None if timeline or rejections else
        "no incident-bearing log events on record for the covered days",
    }


def reconnects(sources, days: Iterable[str] | None = None) -> dict[str, Any]:
    logs = sources.logs
    days = list(days) if days is not None else logs.available_days()
    events = []
    for day in days:
        for e in logs.events(day, kinds=(STREAM_EVENT,)):
            message = str(e.get("msg") or "")
            if "reconnect" in message.lower() or "disconnect" in message.lower() or "error" in message.lower():
                events.append({"ts": e.get("ts"), "day": day, "message": message, "level": e.get("level")})
    return {"events": events, "count": len(events),
            "source": "runtime/logs orb_iex-*.jsonl[.gz], event=stream"}


def websocket_health(sources, days: Iterable[str] | None = None) -> dict[str, Any]:
    logs = sources.logs
    days = list(days) if days is not None else logs.available_days()
    per_day = {}
    last = None
    for day in days:
        events = logs.events(day, kinds=(STREAM_EVENT,))
        per_day[day] = len(events)
        if events:
            last = {"day": day, **events[-1]}
    heartbeat = sources.health.heartbeat() or {}
    return {
        "events_per_day": per_day,
        "last_stream_event": last,
        "stream_recorded": bool(per_day),
        "heartbeat": {k: heartbeat.get(k) for k in ("mode", "phase", "session", "health", "last_data_update",
                                                    "updated_at")},
        "note": ("the trade-update stream is not sampled into the heartbeat, so its health is inferred from "
                 "logged stream events; add a stream heartbeat field to health/heartbeat.json for a live check"
                 if not per_day else None),
    }


def latency(sources, days: Iterable[str] | None = None) -> dict[str, Any]:
    """Entry/exit latency: decision -> submit, submit -> first fill, and total held."""
    logs = sources.logs
    days = list(days) if days is not None else logs.available_days()
    signal_to_submit: list[float] = []
    submit_to_fill: list[float] = []
    for day in days:
        submitted: dict[str, str] = {}
        signals: dict[str, str] = {}
        for e in logs.events(day, kinds=("signal", "order_intent", "fill")):
            kind = e.get("event")
            symbol = e.get("symbol")
            if not symbol:
                continue
            if kind == "signal":
                signals.setdefault(symbol, e.get("ts"))
            elif kind == "order_intent" and e.get("role") == "entry":
                submitted.setdefault(symbol, e.get("ts"))
            elif kind == "fill" and e.get("role") == "entry":
                start = submitted.get(symbol)
                if start:
                    delta = _delta_seconds(start, e.get("ts"))
                    if delta is not None:
                        submit_to_fill.append(delta)
        for symbol, stamp in submitted.items():
            decision = signals.get(symbol)
            delta = _delta_seconds(decision, stamp)
            if delta is not None:
                signal_to_submit.append(delta)
    state = sources.state_journal
    exit_latency: list[float] = []
    for day in days:
        fills = state.fills(day)
        submit_at: dict[str, str] = {}
        for e in logs.events(day, kinds=("order_intent",)):
            if e.get("client_order_id"):
                submit_at[e["client_order_id"]] = e.get("ts")
        for f in fills:
            if f["role"] == "entry":
                continue
            for o in state.orders(day, f["symbol"]):
                if o["order_id"] == f["order_id"]:
                    start = submit_at.get(o["client_order_id"], o.get("created_at"))
                    delta = _delta_seconds(start, f["ts"])
                    if delta is not None:
                        exit_latency.append(delta)
                    break
    return {
        "signal_to_submit_seconds": dist(signal_to_submit),
        "submit_to_first_fill_seconds": dist(submit_to_fill),
        "exit_submit_to_fill_seconds": dist(exit_latency),
        "source": "runtime/logs (signal/order_intent/fill events); timestamps are the bot's own clock",
        "note": "no log events for a stage means no measurement, not zero latency",
    }


def _delta_seconds(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 5. data quality
# ---------------------------------------------------------------------------

def data_quality(sources, trades: Sequence[Trade], days: Iterable[str] | None = None) -> dict[str, Any]:
    logs = sources.logs
    days = list(days) if days is not None else logs.available_days()
    quote_age: list[float] = []
    spread_pct: list[float] = []
    verdicts: dict[str, int] = {}
    per_symbol_spread: dict[tuple[str, str], Decimal] = {}
    for day in days:
        for e in logs.events(day, kinds=(QUOTE_EVENT,)):
            verdicts[e.get("status") or e.get("verdict") or "unknown"] = \
                verdicts.get(e.get("status") or e.get("verdict") or "unknown", 0) + 1
            if e.get("quote_age_seconds") is not None:
                quote_age.append(float(e["quote_age_seconds"]))
            if e.get("spread_pct") is not None and e.get("verdict") == "usable":
                spread_pct.append(float(e["spread_pct"]))
                if e.get("symbol"):
                    per_symbol_spread[(day, e["symbol"])] = D(str(e["spread_pct"]))
    slippage = [float(t.entry_slippage) for t in trades if t.entry_slippage is not None]
    slippage_r = [float(t.entry_slippage / t.one_r_per_share) for t in trades
                  if t.entry_slippage is not None and t.one_r_per_share]
    partial_entry = sum(1 for t in trades if t.raw["entry_fills"] and len(t.raw["entry_fills"]) > 1)
    partial_exit = sum(1 for t in trades if len(t.raw["exit_fills"]) > 1)
    configuration = sources.config
    diagnostics = sources.state_journal.diagnostics(kind="divergence")
    diverged_days = sorted({d["day"] for d in diagnostics if d["kind"] == "divergence"
                            and (d["body"].get("changed") or d["body"].get("vanished")
                                 or d["body"].get("appeared_late"))})
    return {
        "quote_age_seconds": {**dist(quote_age), "limit": configuration.get("max_quote_age_seconds"),
                              "limit_source": "config settings.toml trading.max_quote_age_seconds"},
        "spread_pct": {**dist(spread_pct), "limit": configuration.get("max_quote_spread_pct"),
                       "limit_source": "config settings.toml trading.max_quote_spread_pct"},
        "quote_verdicts": dict(sorted(verdicts.items(), key=lambda kv: -kv[1])),
        "entry_slippage_per_share": dist(slippage),
        "entry_slippage_in_R": dist(slippage_r),
        "partial_fills": {"entry_partial": partial_entry, "exit_partial": partial_exit,
                          "trades": len(trades)},
        "opening_bar_divergence_days": diverged_days,
        "opening_bar_divergence_note": ("the 09:37 diagnostic re-read disagreed with the 09:35 scan on these "
                                       "sessions; signals built on those bars are not reproducible"
                                       if diverged_days else "the 09:37 diagnostic re-read matched the 09:35 scan"),
        "spread_by_trade": {f"{day}:{symbol}": str(value)
                            for (day, symbol), value in per_symbol_spread.items()},
        "delays_by_trade": {t.trade_id: _entry_delay(t, configuration) for t in trades},
        "unavailable": {
            "intraday_price_path": "the journal stores no intra-trade price series, so MFE/MAE use fill prices only",
            "per_session_regime": "no market/volatility regime is recorded per session",
            "order_book_depth": "not available from the free data feed at all",
        },
    }


def _entry_delay(t: Trade, configuration: dict[str, Any]) -> float | None:
    from .metrics import entry_delay_seconds
    delay = entry_delay_seconds(t.day, t.raw["entry_time"], int(configuration.get("scan_delay_seconds", 3)))
    return None if delay is None else round(delay, 3)


# ---------------------------------------------------------------------------
# 6. loss attribution
# ---------------------------------------------------------------------------

def attribution(trades: Sequence[Trade], window_label: str = "all") -> dict[str, Any]:
    classes: dict[str, dict[str, Any]] = {}
    for name in ("normal_strategy", "execution_defect", "data_issue", "broker_issue", "mixed", "unknown"):
        rows = [t for t in trades if t.loss_class == name]
        losses = [t.net for t in rows if t.net is not None and t.net < 0]
        classes[name] = {
            "trades": len(rows),
            "realized_pnl": q(sum((t.realized for t in rows if t.realized is not None), ZERO)),
            "modeled_net_pnl": q(sum((t.net for t in rows if t.net is not None), ZERO)),
            "losing": len(losses),
            "loss_dollars": q(sum(losses, ZERO)),
            "worst": q(min(losses)) if losses else None,
            "symbols": sorted({t.symbol for t in rows}),
        }
    avoided = [t for t in trades if t.net is None]
    return {
        "window": window_label,
        "classes": classes,
        "unclassified": len(avoided),
        "headline": {
            "trades": len(trades),
            "execution_defects": sum(1 for t in trades if t.execution_defects),
            "broker_flags": sum(1 for t in trades if t.broker_flags),
            "data_flags": sum(1 for t in trades if t.data_flags),
            "record_gaps": sum(1 for t in trades if t.record_gaps),
        },
        "rules": ("a trade is execution_defect only when a real defect was recorded (unprotected entry, "
                  "rejected stop, flatten with no stop, crossed entry...). Partial fills, stop replacements "
                  "and end-of-day exits are normal mechanics and never move a loss out of normal_strategy"),
    }


def orders_summary(sources, days: Iterable[str] | None = None) -> dict[str, Any]:
    """Everything the bot did at the broker, by day and role. Evidence, not a summary of P&L."""
    state = sources.state_journal
    orders = state.orders()
    if days is not None:
        keep = set(days)
        orders = [o for o in orders if o["day"] in keep]
    by_day: dict[str, dict[str, int]] = {}
    by_status: dict[str, int] = {}
    by_role: dict[str, int] = {}
    replaces = 0
    for o in orders:
        slot = by_day.setdefault(o["day"], {})
        slot[o["role"]] = slot.get(o["role"], 0) + 1
        by_status[o["status"]] = by_status.get(o["status"], 0) + 1
        by_role[o["role"]] = by_role.get(o["role"], 0) + 1
        replaces += 1 if (o.get("replaces") or o.get("replaced_by")) else 0
    return {"total": len(orders), "by_day": by_day, "by_status": by_status, "by_role": by_role,
            "replaced_orders": replaces,
            "partial_fills": sum(1 for o in orders if (D(str(o.get("filled_qty") or 0)) > 0
                                                       and o.get("status") == "canceled"
                                                       and D(str(o.get("filled_qty") or 0))
                                                       < D(str(o.get("qty") or 0)))),
            "filled_orders": [{"order_id": o["order_id"], "client_order_id": o["client_order_id"],
                               "symbol": o["symbol"], "role": o["role"], "side": o["side"],
                               "qty": o["qty"], "filled_qty": o["filled_qty"],
                               "filled_avg_price": o["filled_avg_price"], "day": o["day"],
                               "created_at": o["created_at"], "updated_at": o["updated_at"]}
                              for o in orders if D(str(o.get("filled_qty") or 0)) > 0]}


def build(sources, trades: Sequence[Trade], *, days: Iterable[str] | None = None) -> dict[str, Any]:
    """The whole Execution & Safety payload."""
    days = list(days) if days is not None else sources.logs.available_days()
    return {
        "state": safety_state(sources),
        "broker_vs_local": broker_vs_local(sources),
        "protection": protection(sources, trades),
        "incidents": incidents(sources, days),
        "reconnects": reconnects(sources, days),
        "websocket": websocket_health(sources, days),
        "latency": latency(sources, days),
        "data_quality": data_quality(sources, trades, days),
        "attribution": attribution(trades),
        "orders": orders_summary(sources, days),
        "days_covered": days,
    }
