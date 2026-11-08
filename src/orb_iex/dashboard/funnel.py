"""Why didn't it trade? The full opportunity funnel for one session.

The gates are exactly the engine's own entry checklist, in the engine's own order
(``orb_iex.tradelog.rule_checklist``). Two sources feed them:

* when the append-only trading journal has the session, its stored rule checklist is
  used verbatim — that is the engine's output, not a re-derivation;
* for earlier sessions the engine's candidate table is fed through the very same
  ``rule_checklist`` function, so the two paths cannot drift.

Every stage reports ``entered`` / ``passed`` / ``failed`` / ``pending`` / ``unknown``
counts, the reasons behind the failures, and the individual symbols, so a healthy
no-trade day is distinguishable from a bot that failed to run.

* ``failed``   the gate was evaluated and said no.
* ``unknown``  the gate was never evaluated because an earlier gate already failed
               (or the rule does not apply, e.g. shorts for a long candidate).
* ``pending``  the gate was not reached and the session itself did not complete, so
               the answer is still owed.
* ``skipped``  not applicable (informational only).

A missing value is never counted as a pass and never counted as a zero.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..tradelog import rule_checklist

D = Decimal
UNKNOWN = "unknown"
PENDING = "pending"

# (rule, label, group, definition) in the engine's evaluation order.
GATES: tuple[tuple[str, str, str, str], ...] = (
    ("opening_bar_present", "Opening bar available", "Data readiness",
     "the 09:30-09:35 IEX opening bar and the required history were loaded"),
    ("relvol_ready", "Relative volume computable", "Data readiness",
     "enough opening-bar history exists to compute relative volume"),
    ("atr_ready", "ATR computable", "Data readiness", "ATR(14) is available"),
    ("adv_ready", "ADV computable", "Data readiness", "average daily volume(14) is available"),
    ("open_gt_5", "Price > $5", "Eligibility", "opening price strictly above $5"),
    ("relvol_ge_1", "RelVol >= 1", "Eligibility",
     "today's opening volume is at least its 14-session average"),
    ("atr_gt_0_50", "ATR > $0.50", "Eligibility", "ATR(14) strictly above $0.50"),
    ("adv_ge_1m", "ADV >= 1M", "Eligibility", "average daily volume(14) at least 1,000,000"),
    ("in_top20", "Ranked in the top 20", "Ranking",
     "rank <= MAX_POSITIONS by relative volume"),
    ("not_flat_bar", "Non-flat opening bar", "Plan quality", "open != close on the opening bar"),
    ("stop_distance_positive", "Positive stop distance", "Plan quality",
     "planned stop differs from the entry trigger"),
    ("qty_positive", "Positive position size", "Plan quality", "sizing produced at least one share"),
    ("disk_space_ok", "Disk space OK", "Preconditions", "free disk above the configured minimum"),
    ("first_signal_of_the_day", "First signal of the day", "Preconditions",
     "no earlier trade was taken in this symbol today"),
    ("kill_switch_off", "Kill switch off", "Preconditions", "no KILL file present"),
    ("no_existing_position_or_order", "No existing position or order", "Preconditions",
     "nothing to collide with in the same symbol"),
    ("short_account_enabled", "Shorting enabled", "Borrow", "the account may short (short candidates only)"),
    ("shortable", "Shortable", "Borrow", "the listing is shortable (short candidates only)"),
    ("easy_to_borrow", "Easy to borrow", "Borrow", "the listing is easy to borrow (short candidates only)"),
    ("buying_power_ok", "Buying power", "Capital", "the order fits the account's buying power"),
    ("before_entry_cutoff", "Before the entry cutoff", "Timing",
     "the entry window (default 10:30 ET) has not closed"),
    ("before_eod_window", "Before the end-of-day window", "Timing",
     "the end-of-day cancel/flatten window has not begun"),
    ("quote_usable", "Quote available", "Quote gate",
     "a usable IEX quote exists (fail closed when absent)"),
    ("spread_within_limit", "Spread within limit", "Quote gate",
     "the quote spread is inside the configured maximum"),
    ("quote_fresh", "Quote fresh", "Quote gate", "the quote is inside the maximum age"),
    ("entry_not_crossed", "Entry not crossed", "Quote gate",
     "the market has not already traded through the trigger"),
    ("broker_accepted", "Broker accepted the order", "Submission", "the broker did not reject it"),
)

GATE_KEYS = tuple(g[0] for g in GATES)
# Contiguous groups of the gate tuple above become the displayed stages.
STAGE_BOUNDARIES: tuple[tuple[str, str], ...] = (
    ("Data readiness", "Universe and inputs loaded"),
    ("Eligibility", "Price / volume / volatility filters"),
    ("Ranking", "Top 20 by relative volume"),
    ("Plan quality", "Entry and stop are tradable"),
    ("Preconditions", "Bot state allows a new entry"),
    ("Borrow", "Shortability (short candidates only)"),
    ("Capital", "Buying power"),
    ("Timing", "Cutoff and end-of-day windows"),
    ("Quote gate", "Fail-closed quote checks"),
    ("Submission", "Broker acknowledgement"),
)

TAIL_STAGES: tuple[tuple[str, str, str], ...] = (
    ("submitted", "Order submitted", "an entry order exists at the broker"),
    ("filled", "Filled", "the entry order filled, fully or partially"),
    ("protected", "Protected", "a protective stop covers the filled quantity"),
    ("closed", "Closed", "the position is flat again"),
)

SYMBOL_LIMIT = 400


class _Opening:
    """Minimal stand-in for marketdata.OpeningBar so rule_checklist can read it."""

    __slots__ = ("o", "h", "l", "c", "v")

    def __init__(self, row: dict):
        self.o = _dec(row.get("open"))
        self.h = _dec(row.get("high"))
        self.l = _dec(row.get("low"))
        self.c = _dec(row.get("close"))
        self.v = row.get("volume")


class _Features:
    __slots__ = ("opening", "relvol", "atr", "adv")

    def __init__(self, row: dict):
        self.opening = _Opening(row) if row.get("open") not in (None, "") else None
        self.relvol = _dec(row.get("relvol"))
        self.atr = _dec(row.get("atr"))
        self.adv = _dec(row.get("adv"))


class _PlanShim:
    """Stands in for an EntryPlan so rule_checklist's broker branch runs as it would live."""

    __slots__ = ("direction",)

    def __init__(self, direction: int | None):
        self.direction = direction if direction is not None else 1


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return D(str(v))
    except Exception:  # noqa: BLE001 - a malformed feature is missing data, not a crash
        return None


def reconstruct_rules(status: str | None, reason: str | None, details: dict) -> dict:
    """Run the engine's own checklist over a candidate's stored features."""
    features = _Features(details or {})
    if reason in ("flat_bar", "zero_stop_distance", "zero_quantity"):
        plan_or_reason: Any = reason
    else:
        plan_or_reason = _PlanShim((details or {}).get("direction"))
    return rule_checklist(features, {"status": status}, plan_or_reason, reason)


# ---------------------------------------------------------------------------
# session inputs
# ---------------------------------------------------------------------------

def _candidate_population(sources, day: str) -> tuple[list[dict], str, list[str]]:
    """(rows, rules_source, notes). Prefers the engine's candidate table for coverage
    and the append-only journal for the stored checklist."""
    notes: list[str] = []
    state_rows = sources.state_journal.candidates(day)
    journal_rows = {(c["day"], c["symbol"]): c for c in sources.trade_journal.candidates(day)}
    rows: list[dict] = []
    for r in state_rows:
        journaled = journal_rows.get((day, r["symbol"]))
        if journaled and isinstance(journaled.get("rules"), dict) and journaled["rules"]:
            rows.append({"symbol": r["symbol"], "rank": r.get("rank"), "status": r.get("status"),
                         "reason": r.get("reason"), "action": journaled.get("action"),
                         "skip_reason": journaled.get("skip_reason"),
                         "rules": journaled["rules"], "rules_source": "journaled",
                         "features": _feature_snapshot(journaled, r.get("details"))})
        else:
            rows.append({"symbol": r["symbol"], "rank": r.get("rank"), "status": r.get("status"),
                         "reason": r.get("reason"),
                         "action": "skip" if r.get("status") not in ("submitted",) else "trade",
                         "skip_reason": r.get("reason"), "rules": reconstruct_rules(
                             r.get("status"), r.get("reason"), r.get("details") or {}),
                         "rules_source": "reconstructed", "features": r.get("details") or {}})
    if not state_rows and journal_rows:
        notes.append("the engine's candidate table is empty for this session; using the append-only journal only")
        for c in journal_rows.values():
            rows.append({"symbol": c["symbol"], "rank": c.get("rank"), "status": None,
                         "reason": c.get("skip_reason"), "action": c.get("action"),
                         "skip_reason": c.get("skip_reason"),
                         "rules": c.get("rules") or {}, "rules_source": "journaled",
                         "features": _feature_snapshot(c, {})})
    if not rows:
        notes.append("no candidate rows exist for this session: the funnel is unknown, not empty")
    journaled_n = sum(1 for r in rows if r["rules_source"] == "journaled")
    rebuilt_n = len(rows) - journaled_n
    if rebuilt_n:
        notes.append(f"{journaled_n} of {len(rows)} candidate row(s) carry the engine's stored rule checklist; "
                     f"the other {rebuilt_n} were re-derived with the very same rule_checklist function because "
                     "the append-only trading journal does not cover this session")
    if not rows:
        sources_label = "none"
    else:
        sources_label = "journaled" if not rebuilt_n else "mixed" if journaled_n else "reconstructed"
    return rows, sources_label, notes


def _feature_snapshot(journaled: dict, fallback: dict | None) -> dict:
    out = {k: journaled.get(k) for k in ("open", "high", "low", "close", "volume", "relvol", "atr", "adv")}
    out["direction"] = journaled.get("direction")
    if not any(out.get(k) for k in ("open", "relvol", "atr", "adv")):
        for k, v in (fallback or {}).items():
            if k in out and v is not None:
                out[k] = v
    return out


# ---------------------------------------------------------------------------
# funnel
# ---------------------------------------------------------------------------

def build(sources, day: str) -> dict[str, Any]:
    """The complete funnel for one session, with per-symbol detail at every stage."""
    state = sources.state_journal
    rows, rules_source, notes = _candidate_population(sources, day)
    day_row = state.day(day)
    orders = state.orders(day)
    fills = state.fills(day)
    trades = {t["symbol"]: t for t in state.trades(day)}
    scan_ran = bool(day_row and day_row["scanned"] and day_row["scan_status"] == "ok")
    if day_row is None:
        notes.append("the engine's session table has no row for this day")

    # -- gate stages ----------------------------------------------------------
    order = {key: i for i, key in enumerate(GATE_KEYS)}
    stages: list[dict[str, Any]] = []
    # universe stage: everything the bot attempted
    stages.append(_stage("attempted_universe", "Attempted listings", "Universe",
                         "every symbol the bot tried to load opening bars and history for",
                         entered=len(rows), passed=len(rows), failed=0, pending=0, unknown=0,
                         reasons={}, symbols_passed=[r["symbol"] for r in rows], symbols_failed=[],
                         extra={"unavailable": None if rows else "no candidate rows recorded"}))

    for group, group_label in STAGE_BOUNDARIES:
        group_gates = [g for g in GATES if g[2] == group]
        entered_syms: list[str] = []
        passed: list[str] = []
        failed: list[dict] = []
        unknown: list[dict] = []
        pending: list[dict] = []
        reasons: dict[str, int] = {}
        for r in rows:
            rules = r["rules"] or {}
            # entered = no earlier gate said no. A gate that does not apply (a long
            # standing in front of the borrow checks) is unknown here, not a stop.
            before = [k for k in GATE_KEYS[:order[group_gates[0][0]]]]
            if any(rules.get(k) is False for k in before):
                continue
            entered_syms.append(r["symbol"])
            verdict = None
            for gate in group_gates:
                value = rules.get(gate[0])
                if value is False:
                    verdict = ("failed", gate, r)
                    break
                if value is None:
                    verdict = ("unknown", gate, r)
                    break
            if verdict is None:
                passed.append(r["symbol"])
                continue
            kind, gate, row = verdict
            entry = {"symbol": row["symbol"], "gate": gate[0],
                     "reason": row.get("skip_reason") or row.get("reason"),
                     "rank": row.get("rank")}
            if kind == "failed":
                failed.append(entry)
                label = entry["reason"] or gate[0]
                reasons[str(label)] = reasons.get(str(label), 0) + 1
            else:
                # not evaluated: pending only if the session never finished the scan
                (pending if (not scan_ran and row.get("rank") is None) else unknown).append(entry)
        stages.append(_stage(group_key(group), group_label, group,
                             "; ".join(f"{g[1]}: {g[3]}" for g in group_gates),
                             entered=len(entered_syms), passed=len(passed), failed=len(failed),
                             pending=len(pending), unknown=len(unknown),
                             reasons=reasons, symbols_passed=passed,
                             symbols_failed=failed[:SYMBOL_LIMIT],
                             extra={"unknown_symbols": [e["symbol"] for e in unknown[:SYMBOL_LIMIT]],
                                    "pending_symbols": [e["symbol"] for e in pending[:SYMBOL_LIMIT]],
                                    "gates": [{"key": g[0], "label": g[1], "definition": g[3]}
                                              for g in group_gates]}))

    # -- signal ---------------------------------------------------------------
    # broker_accepted is the last gate rule_checklist can set True, so it is the exact
    # marker of "every applicable rule passed and the order went out".
    signalled = [r for r in rows if (r["rules"] or {}).get("broker_accepted") is True]
    stages.append(_stage("signal", "Signal", "Signal",
                         "every entry rule passed; the engine issued an order intent",
                         entered=len(rows), passed=len(signalled), failed=len(rows) - len(signalled),
                         pending=0, unknown=0,
                         reasons=_reason_counts(rows, signalled),
                         symbols_passed=[r["symbol"] for r in signalled],
                         symbols_failed=[{"symbol": r["symbol"],
                                          "gate": _first_failed_gate(r["rules"]),
                                          "reason": r.get("skip_reason") or r.get("reason")}
                                         for r in rows if r not in signalled][:SYMBOL_LIMIT]))

    # -- derived tail ---------------------------------------------------------
    entries_by_symbol: dict[str, list[dict]] = {}
    for o in orders:
        if o["role"] == "entry":
            entries_by_symbol.setdefault(o["symbol"], []).append(o)
    filled_by_symbol: dict[str, Decimal] = {}
    for f in fills:
        if f["role"] == "entry":
            filled_by_symbol[f["symbol"]] = filled_by_symbol.get(f["symbol"], D(0)) + D(f["qty"])
    stops_by_symbol: dict[str, list[dict]] = {}
    for o in orders:
        if o["role"] == "stop":
            stops_by_symbol.setdefault(o["symbol"], []).append(o)

    def _tail(key: str, label: str, group: str, definition: str, decider) -> dict[str, Any]:
        passed, failed, pending, unknown, reasons = [], [], [], [], {}
        for r in signalled:
            symbol = r["symbol"]
            state_ = decider(symbol)
            if state_ is True:
                passed.append(symbol)
            elif state_ is False:
                failed.append({"symbol": symbol, "gate": key, "reason": None})
            elif state_ == PENDING:
                pending.append({"symbol": symbol, "gate": key, "reason": "the session did not complete"})
            else:
                unknown.append({"symbol": symbol, "gate": key, "reason": "no record at this stage"})
        return _stage(key, label, group, definition, entered=len(signalled), passed=len(passed),
                      failed=len(failed), pending=len(pending), unknown=len(unknown), reasons=reasons,
                      symbols_passed=passed, symbols_failed=failed,
                      extra={"unknown_symbols": [e["symbol"] for e in unknown],
                             "pending_symbols": [e["symbol"] for e in pending]})

    def submitted(symbol: str):
        if symbol in entries_by_symbol:
            return True
        return PENDING if not scan_ran else False

    def filled(symbol: str):
        if symbol in filled_by_symbol:
            return True
        if symbol not in entries_by_symbol:
            return None
        return PENDING if not scan_ran else False

    def protected(symbol: str):
        qty = filled_by_symbol.get(symbol)
        if not qty:
            return None
        stops = stops_by_symbol.get(symbol) or []
        covered = max((D(o.get("qty") or 0) for o in stops), default=D(0))
        row = trades.get(symbol)
        if row and D(row.get("protected_qty") or 0) > 0:
            return True
        if not stops:
            return PENDING if not scan_ran else False
        return covered >= qty

    def closed(symbol: str):
        if symbol not in filled_by_symbol:
            return None                       # nothing was ever filled: closing does not apply
        row = trades.get(symbol)
        if row is not None:
            return bool(row.get("closed"))
        # No engine trade row for this symbol: fall back to the fills themselves, which
        # are the strongest evidence anyway, before reporting anything as not closed.
        exited = sum((D(f["qty"]) for f in fills
                      if f["symbol"] == symbol and f["role"] != "entry"), D(0))
        if exited == filled_by_symbol[symbol]:
            return True
        return PENDING if not scan_ran else False

    stages.append(_tail("submitted", "Order submitted", "Execution",
                        "an entry order exists at the broker for this signal", submitted))
    stages.append(_tail("filled", "Filled", "Execution",
                        "the entry order filled, fully or partially", filled))
    stages.append(_tail("protected", "Protected", "Execution",
                        "a protective stop covers the filled quantity", protected))
    stages.append(_tail("closed", "Closed", "Execution", "the position is flat again", closed))

    summary = {
        "day": day,
        "attempted": len(rows),
        "signals": len(signalled),
        "submitted": sum(1 for s in signalled if s["symbol"] in entries_by_symbol),
        "filled": len(filled_by_symbol),
        "protected": sum(1 for s in filled_by_symbol
                         if max((D(o.get("qty") or 0) for o in stops_by_symbol.get(s, [])), default=D(0))
                         >= filled_by_symbol[s]),
        "closed": sum(1 for s in filled_by_symbol if closed(s) is True),
        "diagnosis": diagnose(day, day_row, rows, signalled, filled_by_symbol, scan_ran),
    }
    return {"day": day, "scan_ran": scan_ran, "rules_source": rules_source, "stages": stages,
            "summary": summary, "notes": notes,
            "universe_meta": next((m for m in state.universe_months()
                                   if m["month"] == day[:7]), None)}


def diagnose(day: str, day_row: dict | None, rows: list[dict], signalled: list[dict],
             filled: dict, scan_ran: bool) -> dict[str, Any]:
    """The one-line answer: did the bot trade, and if not, why not.

    Separates a healthy no-trade day (the rules said no) from a bot failure
    (the bot never ran, or ran and could not reach the market).
    """
    if day_row is None:
        return {"state": "unavailable", "headline": "No session record for this day",
                "detail": "the engine never wrote a row for this day; nothing can be concluded"}
    if not rows:
        return {"state": "unavailable", "headline": "No candidate data recorded",
                "detail": "without a candidate list the funnel is unknown, not empty"}
    if not scan_ran:
        return {"state": "not_run",
                "headline": f"Scan did not complete (status {day_row['scan_status']!r})",
                "detail": "no entry decision was made; this is a bot/session issue, not a strategy verdict"}
    if not signalled:
        return {"state": "healthy_no_trade",
                "headline": "Healthy no-trade day: the rules excluded every candidate",
                "detail": f"{len(rows)} listings attempted and every one was filtered out before an entry"}
    if not filled:
        return {"state": "no_fill",
                "headline": f"{len(signalled)} signal(s), none filled",
                "detail": "entries were submitted and did not fill (price never reached the trigger, or "
                          "they were cancelled at the cutoff) — a strategy outcome, not a failure"}
    return {"state": "traded", "headline": f"{len(filled)} filled position(s) out of {len(signalled)} signal(s)",
            "detail": "see the Trades and Execution pages for how each one resolved"}


def group_key(group: str) -> str:
    return group.lower().replace(" ", "_").replace("-", "_")


def _stage(key, label, group, definition, *, entered, passed, failed, pending, unknown, reasons,
           symbols_passed, symbols_failed, extra=None) -> dict[str, Any]:
    truncated = len(symbols_passed) > SYMBOL_LIMIT or len(symbols_failed) > SYMBOL_LIMIT
    return {
        "key": key, "label": label, "group": group, "definition": definition,
        "entered": entered, "passed": passed, "failed": failed, "pending": pending, "unknown": unknown,
        "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "symbols_passed": list(symbols_passed)[:SYMBOL_LIMIT],
        "symbols_failed": list(symbols_failed)[:SYMBOL_LIMIT],
        "symbols_truncated": truncated,
        "drop": entered - passed,
        **(extra or {}),
    }


def _reason_counts(rows: list[dict], passed: list[dict]) -> dict[str, int]:
    keep = {r["symbol"] for r in passed}
    reasons: dict[str, int] = {}
    for r in rows:
        if r["symbol"] in keep:
            continue
        label = r.get("skip_reason") or r.get("reason")
        if not label:
            gate = _first_failed_gate(r["rules"])
            label = gate or "no reason recorded"
        reasons[str(label)] = reasons.get(str(label), 0) + 1
    return dict(sorted(reasons.items(), key=lambda kv: -kv[1]))


def _first_failed_gate(rules: dict) -> str | None:
    for key in GATE_KEYS:
        if (rules or {}).get(key) is False:
            return key
    return None


def days(sources) -> list[str]:
    """Every day a funnel can be built for, newest first."""
    out = {d["day"] for d in sources.state_journal.days()}
    out |= set(sources.trade_journal.candidate_days())
    return sorted(out, reverse=True)
