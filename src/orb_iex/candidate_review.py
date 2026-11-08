"""Read-only daily A/B evidence report. Does not qualify or modify a strategy."""
import argparse
from collections import Counter
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sqlite3


def review(path: str | Path, day: date) -> dict:
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        diagnostics = {r["kind"]: json.loads(r["body"]) for r in db.execute(
            "SELECT kind,body FROM diagnostics WHERE day=? AND (kind='ab_plans' OR kind LIKE 'ab_quote:%')", (day.isoformat(),))}
        candidates = [dict(r) for r in db.execute(
            "SELECT symbol,status,reason FROM candidates WHERE day=?", (day.isoformat(),))]
        fills = [dict(r) for r in db.execute(
            "SELECT symbol,role,side,qty,price,commission,slippage,ts FROM fills WHERE day=?", (day.isoformat(),))]
        session = db.execute('SELECT scan_status,done FROM days WHERE day=?', (day.isoformat(),)).fetchone()
    plans = diagnostics.get("ab_plans", {})
    quotes = {k.split(":", 1)[1]: v for k, v in diagnostics.items() if k.startswith("ab_quote:")}
    reasons = Counter(v["ab"].get("candidate_reason") or "eligible_at_submission" for v in quotes.values())
    outcomes = {}
    for f in fills:
        outcomes.setdefault(f["symbol"], []).append(f)
    evidence = {}
    for symbol, rows in outcomes.items():
        roles = {r["role"] for r in rows}
        exits = roles - {"entry"}
        if exits and all(role.startswith("stop") and not role.startswith("stopfail") for role in exits):
            label = "stop_exit_observed; ordinary_loss_vs_process_failure_unresolved"
        elif exits == {"eod1"} or exits == {"eod"}:
            label = "scheduled_exit_observed"
        elif exits:
            label = "recovery_or_other_exit; inspect_process_evidence"
        else:
            label = "no_exit_evidence"
        quantity = sum((Decimal(r["qty"]) * (1 if r["side"] == "buy" else -1) for r in rows), Decimal(0))
        cash = sum((Decimal(r["qty"]) * Decimal(r["price"]) * (-1 if r["side"] == "buy" else 1)
                    for r in rows), Decimal(0))
        costs = sum((Decimal(r["commission"]) + Decimal(r["slippage"]) for r in rows), Decimal(0))
        closed = quantity == 0 and "entry" in roles and bool(exits)
        evidence[symbol] = {"roles": sorted(roles), "classification": label,
                            "net_quantity": str(quantity), "gross_closed_cash": str(cash) if closed else None,
                            "modeled_cost": str(costs),
                            "net_closed_cash_1x": str(cash - costs) if closed else None,
                            "net_closed_cash_2x": str(cash - 2 * costs) if closed else None,
                            "failed_breakout_observed": closed and cash < 0 and any(r.startswith("stop") for r in exits),
                            "first_fill_time": min(r["ts"] for r in rows),
                            "last_fill_time": max(r["ts"] for r in rows)}
    status = 'COLLECTING' if plans else 'INCOMPLETE'
    if session and session['scan_status'] == 'ok':
        completed = 'post_close' in json.loads(session['done'] or '{}')
        status = ('COMPLETE' if plans else 'COMPLETE_NO_SIGNALS') if completed else 'COLLECTING'
    return {"day": day.isoformat(), "status": status,
            "scan_status": session['scan_status'] if session else None,
            "paired_plans": plans, "quote_decisions": quotes,
            "candidate_reasons": dict(reasons), "all_signal_decisions": candidates,
            "outcome_evidence": evidence,
            "hypotheses": [{"observation": reason, "count": count,
                            "next_check": "compare paired future outcomes including excluded winners; no retuning"}
                           for reason, count in sorted(reasons.items()) if reason != "eligible_at_submission"],
            "qualification": "BLOCKED: point-in-time execution replay and untouched forward evidence required",
            "regime_stability": "unknown; no regime filter or proxy inference"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--day", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    print(json.dumps(review(args.journal, args.day), indent=2))


if __name__ == "__main__":
    main()
