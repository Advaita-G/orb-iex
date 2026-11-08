"""Run ORB specs over the stored data: select -> simulate on stored 1-minute paths -> size -> metrics."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date

import pandas as pd

from . import alpaca_data as ad
from .matrix import SELECTION_SPECS, SPECS  # noqa: F401
from .orb import BASE_COSTS, Costs, OrbSpec, Sizing, path_arrays, run_portfolio, simulate_arrays
from .pipeline import select_all

DATA = ad.DATA_DIR
REPLICATION = (pd.Timestamp("2016-01-04"), pd.Timestamp("2023-12-29"))
HOLDOUT_START = pd.Timestamp("2024-01-02")


def stage_path_lists() -> None:
    sel = select_all({k: SPECS[k] for k in SELECTION_SPECS})
    by_day: dict[str, set] = defaultdict(set)
    for name, df in sel.items():
        df.to_parquet(DATA / f"selected_{name}.parquet")
        for d, s in zip(df.date, df.symbol):
            by_day[d.date().isoformat()].add(s)
    out = {k: sorted(v) for k, v in by_day.items()}
    (DATA / "path_lists.json").write_text(json.dumps(out))
    n = [len(v) for v in out.values()]
    print(f"{len(out)} sessions, {sum(n)} symbol-days, median {sorted(n)[len(n)//2]}/session")


def stage_fetch_paths() -> None:
    lists = json.loads((DATA / "path_lists.json").read_text())
    ad.fetch_paths({date.fromisoformat(k): v for k, v in lists.items()})


def simulate_jobs(jobs: dict[str, tuple[OrbSpec, Costs, str]]) -> dict[str, pd.DataFrame]:
    """One pass over the stored sessions for many (spec, costs, candidate-set) jobs. Returns executed trades per job.
    The candidate set is a SELECTION_SPECS entry; variants that only change stops/cutoffs/exits reuse B0's set and are
    post-filtered by RVOL only (B0's ranking is untouched)."""
    sets: dict[str, pd.DataFrame] = {}
    cache: dict[str, pd.DataFrame] = {}
    for n, (spec, _, sel) in jobs.items():
        if sel not in cache:
            cache[sel] = pd.read_parquet(DATA / f"selected_{sel}.parquet")
        d = cache[sel]
        sets[n] = {k: g for k, g in d[d.rvol >= spec.min_rvol].groupby("date")}
    out: dict[str, list] = {n: [] for n in jobs}
    days = sorted({d for m in sets.values() for d in m})
    for day in days:
        pf = DATA / "paths" / f"{pd.Timestamp(day).date().isoformat()}.parquet"
        if not pf.exists():
            continue
        paths = pd.read_parquet(pf)
        arrays = {sym: path_arrays(g.sort_values("t")) for sym, g in paths.groupby("symbol")}
        for n, (spec, costs, _) in jobs.items():
            cand = sets[n].get(day)
            if cand is None:
                continue
            for r in cand.itertuples():
                a = arrays.get(r.symbol)
                if a is None:
                    continue
                d = int(r.direction)
                level = r.or_high if d == 1 else r.or_low
                extreme = r.or_low if d == 1 else r.or_high
                t = simulate_arrays(a, d, float(level), float(r.atr14), float(extreme), spec, costs)
                if t:
                    out[n].append({"date": day, "symbol": r.symbol, "direction": d, "rvol": r.rvol, "rvol_base": r.rvol_base,
                                   "atr14": r.atr14, "adv14": r.adv14, "prev_close": r.prev_close, "or_vol": r.or_vol,
                                   "etf_like": r.etf_like, **t})
    return {n: pd.DataFrame(v) for n, v in out.items()}


def evaluate(trades: pd.DataFrame, sizing: Sizing, sessions: pd.DatetimeIndex, lo: pd.Timestamp, hi: pd.Timestamp):
    from .metrics import summarize
    t = trades[(trades.date >= lo) & (trades.date <= hi)]
    rows, curve = run_portfolio(t, sizing)
    sess = sessions[(sessions >= lo) & (sessions <= hi)]
    return rows, curve, summarize(curve, rows, sess, sizing.start_equity)


if __name__ == "__main__":
    import sys
    {"path_lists": stage_path_lists, "fetch_paths": stage_fetch_paths}[sys.argv[1]]()
