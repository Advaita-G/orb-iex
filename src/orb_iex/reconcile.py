"""Trade reconstruction from RAW broker fills (the source of truth) and comparison with the journal.

Read-only: never writes to the broker. Definitions (one meaning each, never mixed):
  gross_pnl / actual_realized_pnl  cash result of the broker fills; entry and exit prices are the real
                                   fill prices, so REAL slippage is already inside them. Identical by definition.
  modeled_commission / modeled_slippage  hypothetical per-share costs ON TOP of the fills (not paid at the broker;
                                   the modeled slippage is not the slippage already realized in the fill prices).
  net_modeled_pnl = gross_pnl - modeled_commission - modeled_slippage
A trade = a bot-owned position from flat to flat, across days (carry-overs included). A fill that crosses
zero is split into a close and a new open (flagged sign_flip). Positions still open are reported, not dropped.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from decimal import Decimal as D
from pathlib import Path

BOT_PREFIX = "orbiex-"


def fills_from_orders(orders):
    """Bot-owned filled orders as time-ordered fills; cancelled/replaced orders keep their partial fills."""
    out = []
    for o in orders:
        if not str(o.get("client_order_id", "")).startswith(BOT_PREFIX):
            continue
        qty = D(o.get("filled_qty") or 0)
        if qty <= 0:
            continue
        if not o.get("filled_at") or not o.get("filled_avg_price"):
            raise ValueError(f"filled order {o.get('id')} lacks filled_at/filled_avg_price")
        out.append({"id": o["id"], "symbol": o["symbol"], "ts": o["filled_at"], "qty": qty,
                    "signed": qty if o["side"] == "buy" else -qty, "price": D(o["filled_avg_price"]),
                    "role": o["client_order_id"].rsplit("-", 1)[-1]})
    out.sort(key=lambda f: (f["ts"], f["id"]))
    return out


def _trip(symbol, direction, first):
    return {"symbol": symbol, "direction": direction, "entry_fills": [], "exit_fills": [], "sign_flip": False,
            "entry_time": first["ts"], "exit_time": None, "order_ids": set()}


def _close(t, risk_per_share, commission, slippage):
    eq = sum(f["qty"] for f in t["entry_fills"])
    xq = sum(f["qty"] for f in t["exit_fills"])
    ev = sum(f["qty"] * f["price"] for f in t["entry_fills"]) / eq
    xv = sum(f["qty"] * f["price"] for f in t["exit_fills"]) / xq if xq else None
    gross = t["direction"] * (xv - ev) * xq if xq else D(0)
    mc, ms = commission * (eq + xq), slippage * (eq + xq)
    risk = risk_per_share * eq if risk_per_share else None
    return {"symbol": t["symbol"], "day": t["entry_time"][:10], "direction": t["direction"], "qty": eq,
            "entry_vwap": ev, "exit_vwap": xv, "entry_time": t["entry_time"], "exit_time": t["exit_time"],
            "carry": bool(t["exit_time"]) and t["exit_time"][:10] != t["entry_time"][:10],
            "sign_flip": t["sign_flip"], "gross_pnl": gross, "actual_realized_pnl": gross,
            "modeled_commission": mc, "modeled_slippage": ms, "modeled_cost": mc + ms,
            "net_modeled_pnl": gross - mc - ms, "initial_risk": risk,
            "r": gross / risk if risk else None, "open_qty": eq - xq, "order_ids": sorted(t["order_ids"])}


def fills_from_journal(rows):
    """State-journal fill rows (one row per fill increment) in the same shape as fills_from_orders."""
    out = [{"id": r["order_id"], "symbol": r["symbol"], "ts": r["ts"], "qty": D(r["qty"]),
            "signed": D(r["qty"]) if r["side"] == "buy" else -D(r["qty"]), "price": D(r["price"]),
            "role": r["role"]} for r in rows if r["price"] is not None and D(r["qty"]) > 0]
    out.sort(key=lambda f: (f["ts"], f["id"]))
    return out


def reconstruct(orders=None, commission=D("0.0035"), slippage=D("0.01"), risk_per_share=None, fills=None):
    """-> (closed_trades, open_trades). risk_per_share: {(day, symbol): D} from the trade plan."""
    risk_per_share = risk_per_share or {}
    pos, cur, closed, opened = {}, {}, [], []
    for f in (fills if fills is not None else fills_from_orders(orders)):
        s = f["symbol"]
        rem = f["qty"]
        while rem > 0:
            p = pos.get(s, D(0))
            sign = f["signed"] / f["qty"]
            part = {**f, "qty": rem}
            if p == 0 or (p > 0) == (sign > 0):
                if p == 0:
                    cur[s] = _trip(s, int(sign), f)
                cur[s]["entry_fills"].append(part)
                cur[s]["order_ids"].add(f["id"])
                pos[s] = p + sign * rem
                rem = D(0)
            else:
                take = min(rem, abs(p))
                cur[s]["exit_fills"].append({**f, "qty": take})
                cur[s]["order_ids"].add(f["id"])
                pos[s] = p + sign * take
                rem -= take
                if pos[s] == 0:
                    t = cur.pop(s)
                    t["exit_time"] = f["ts"]
                    t["sign_flip"] = rem > 0
                    closed.append(_close(t, risk_per_share.get((t["entry_time"][:10], s)), commission, slippage))
    for s, t in cur.items():
        opened.append(_close(t, risk_per_share.get((t["entry_time"][:10], s)), commission, slippage))
    closed.sort(key=lambda r: (r["entry_time"], r["symbol"]))
    return closed, opened


def totals(trips):
    keys = ("gross_pnl", "actual_realized_pnl", "modeled_commission", "modeled_slippage", "modeled_cost",
            "net_modeled_pnl")
    return {k: sum((t[k] for t in trips), D(0)) for k in keys}


def journal_trips(outcomes):
    """Group tradelog outcome rows into trades: an open_at_close row + the later carry row are ONE trade.
    Costs count once (the carry row already includes the entry-side costs of the open_at_close row)."""
    by_sym, trips = {}, []
    for o in sorted(outcomes, key=lambda o: (o["day"], o.get("entry_time") or "")):
        pend = by_sym.get(o["symbol"])
        if o.get("status") == "open_at_close":
            by_sym.setdefault(o["symbol"], []).append(o)
            continue
        entry_day = o["day"]
        if o.get("entry_time") is None and pend:          # carry-over exit row
            entry_day = pend.pop(0)["day"]
        trips.append({**o, "entry_day": entry_day})
    for sym, rows in by_sym.items():
        trips += [{**r, "entry_day": r["day"], "unclosed": True} for r in rows]
    return trips


def compare(closed, opened, outcomes, tol=D("0.01")):
    """Every difference is reported; nothing is silently omitted. Returns a list of mismatch dicts."""
    issues, jt = [], journal_trips(outcomes)
    used = set()
    for t in closed:
        m = next((i for i, j in enumerate(jt) if i not in used and j["symbol"] == t["symbol"]
                  and j["entry_day"] == t["day"] and not j.get("unclosed")), None)
        if m is None:
            issues.append({"kind": "missing_in_journal", "symbol": t["symbol"], "day": t["day"],
                           "gross_pnl": str(t["gross_pnl"])})
            continue
        used.add(m)
        j = jt[m]
        for k_b, k_j in (("gross_pnl", "gross_pnl"), ("net_modeled_pnl", "net_pnl_modeled"),
                         ("modeled_cost", None)):
            if k_j is None:
                jv = D(j["modeled_commission"] or 0) + D(j["modeled_slippage"] or 0)
            else:
                jv = D(j[k_j]) if j.get(k_j) not in (None, "") else None
            if jv is None or abs(jv - t[k_b]) > tol:
                issues.append({"kind": "value_mismatch", "field": k_b, "symbol": t["symbol"], "day": t["day"],
                               "broker": str(t[k_b]), "journal": None if jv is None else str(jv)})
    for i, j in enumerate(jt):
        if i not in used and not j.get("unclosed"):
            issues.append({"kind": "missing_at_broker", "symbol": j["symbol"], "day": j["entry_day"]})
    open_syms = {o["symbol"] for o in opened}
    for j in jt:
        if j.get("unclosed") and j["symbol"] not in open_syms:
            issues.append({"kind": "journal_open_but_broker_flat", "symbol": j["symbol"], "day": j["entry_day"]})
    return issues


def compare_trips(broker, journal, tol=D("0.01")):
    """Broker-fill trips vs trips rebuilt from journal fills; every difference is listed."""
    issues, remaining = [], list(journal)
    for t in broker:
        match = next((i for i, j in enumerate(remaining)
                      if j["symbol"] == t["symbol"] and j["day"] == t["day"]
                      and j["order_ids"] == t["order_ids"]), None)
        j = remaining.pop(match) if match is not None else None
        if j is None:
            issues.append({"kind": "missing_in_journal", "symbol": t["symbol"], "day": t["day"],
                           "gross_pnl": str(t["gross_pnl"])})
            continue
        for k in ("qty", "gross_pnl", "net_modeled_pnl", "entry_vwap", "exit_vwap"):
            if abs(t[k] - j[k]) > tol:
                issues.append({"kind": "value_mismatch", "field": k, "symbol": t["symbol"], "day": t["day"],
                               "broker": str(t[k]), "journal": str(j[k])})
    issues += [{"kind": "missing_at_broker", "symbol": j["symbol"], "day": j["day"]}
               for j in remaining]
    return issues


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshot", type=Path, required=True, help="JSON with an 'orders' list from the broker")
    p.add_argument("--journal", type=Path, help="state journal sqlite (fills table); compares trades when given")
    a = p.parse_args()
    closed, opened = reconstruct(json.loads(a.snapshot.read_text())["orders"])
    rep = {"closed": len(closed), "open": [(o["symbol"], str(o["open_qty"])) for o in opened],
           "totals": {k: str(v) for k, v in totals(closed).items()}}
    if a.journal:
        db = sqlite3.connect(f"file:{a.journal}?mode=ro", uri=True)
        cur = db.execute("SELECT * FROM fills")
        names = [d[0] for d in cur.description]
        jc, jo = reconstruct(fills=fills_from_journal([dict(zip(names, r)) for r in cur]))
        rep["journal_totals"] = {k: str(v) for k, v in totals(jc).items()}
        rep["issues"] = compare_trips(closed, jc)
    print(json.dumps(rep, indent=2))


if __name__ == "__main__":
    main()
