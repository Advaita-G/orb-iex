"""What does using IEX opening data (the production feed) cost relative to consolidated SIP data?

For the most recent sessions: rebuild the opening-range table from IEX 1-minute bars, select the top 20 the way the bot
would, and compare with the SIP selection (overlap, direction agreement, RVOL rank agreement). Then trade both
selections on the same SIP 1-minute paths (IEX levels for the IEX selection) to compare expectancy.
"""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd

from . import alpaca_data as ad
from .backtest import DATA
from .orb import BASE_COSTS, OrbSpec, add_rvol, opening_ranges, path_arrays, select_candidates, simulate_arrays

N_SESSIONS = 60
HISTORY = 16


def fetch(n: int = N_SESSIONS) -> None:
    lists = json.loads((DATA / "open_lists.json").read_text())
    days = sorted(lists)[-(n + HISTORY):]
    ad.fetch_openings_feed({date.fromisoformat(k): lists[k] for k in days}, "iex", DATA / "open1m_iex", minutes=5)


def iex_openings() -> pd.DataFrame:
    parts = []
    for p in sorted((DATA / "open1m_iex").glob("*.parquet")):
        b = pd.read_parquet(p)
        if len(b):
            parts.append(opening_ranges(b, 5))
    t = add_rvol(pd.concat(parts, ignore_index=True), 14, 14)               # production needs a full 14
    f = pd.read_parquet(DATA / "openings_5m.parquet")[["symbol", "date", "prev_close", "atr14", "adv14", "split_window",
                                                        "etf_like"]]
    return t.merge(f, on=["symbol", "date"], how="inner")


def compare(n: int = N_SESSIONS) -> dict:
    iex = iex_openings()
    days = sorted(iex.date.unique())[-n:]
    sip = pd.read_parquet(DATA / "selected_B0.parquet")
    sip = sip[sip.date.isin(days)]
    spec = OrbSpec()
    rows, iex_sel = [], []
    for d in days:
        s = select_candidates(iex[iex.date == d], spec).assign(date=d)
        iex_sel.append(s)
        a = set(s.symbol)
        b = set(sip[sip.date == d].symbol)
        both = a & b
        dir_a = s.set_index("symbol").direction
        dir_b = sip[sip.date == d].set_index("symbol").direction
        rows.append({"date": d, "iex_n": len(a), "sip_n": len(b), "overlap": len(both),
                     "dir_agree": int(sum(dir_a[x] == dir_b[x] for x in both))})
    iex_sel = pd.concat(iex_sel, ignore_index=True)
    r = pd.DataFrame(rows)
    out = {"sessions": len(days), "mean_overlap_of_20": float(r.overlap.mean()),
           "direction_agreement_on_overlap": float(r.dir_agree.sum() / max(r.overlap.sum(), 1)),
           "mean_iex_candidates": float(r.iex_n.mean())}
    # trade the IEX selection (IEX levels) and the SIP selection (SIP levels) on SIP paths
    client = ad.make_client()
    res = {"iex": [], "sip": []}
    for d in days:
        sel_i = iex_sel[iex_sel.date == d]
        sel_s = sip[sip.date == d]
        syms = sorted(set(sel_i.symbol) | set(sel_s.symbol))
        pf = DATA / "paths" / f"{pd.Timestamp(d).date().isoformat()}.parquet"
        paths = pd.read_parquet(pf) if pf.exists() else pd.DataFrame(columns=["symbol"])
        missing = [s for s in syms if s not in set(paths.symbol)]
        if missing:
            extra = ad.fetch_window(client, missing, pd.Timestamp(d).date(), (9, 30), (16, 0), chunk=100)
            paths = pd.concat([paths, extra], ignore_index=True)
        arrays = {s: path_arrays(g.sort_values("t")) for s, g in paths.groupby("symbol")}
        for label, sel in (("iex", sel_i), ("sip", sel_s)):
            for q in sel.itertuples():
                a = arrays.get(q.symbol)
                if a is None:
                    continue
                dr = int(q.direction)
                level = q.or_high if dr == 1 else q.or_low
                ext = q.or_low if dr == 1 else q.or_high
                for mode in ("pessimistic", "optimistic"):
                    t = simulate_arrays(a, dr, float(level), float(q.atr14), float(ext),
                                        OrbSpec(same_bar=mode), BASE_COSTS)
                    if t:
                        res[label].append({"date": d, "symbol": q.symbol, "mode": mode, "r": t["r"], "reason": t["reason"]})
    for label in ("iex", "sip"):
        df = pd.DataFrame(res[label])
        out[label] = {m: {"trades": int(len(g)), "exp_r": float(g.r.mean()), "win": float((g.r > 0).mean())}
                      for m, g in df.groupby("mode")} if len(df) else {}
    (DATA.parent / "iex_compare.json").write_text(json.dumps(out, indent=1, default=float))
    return out


if __name__ == "__main__":
    import sys
    {"fetch": fetch, "compare": compare}[sys.argv[1]]()
