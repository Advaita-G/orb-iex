"""Produce every ORB result table from the stored data. Output: runtime/research/orb_results.json + printed summary.

Run: python -m quantlab.report [baseline|matrix|segments|all]. The holdout (2024+) is evaluated for every
preregistered spec exactly once, in the `matrix` stage, after the replication numbers are written.
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace

import numpy as np
import pandas as pd

from . import alpaca_data as ad
from .backtest import HOLDOUT_START, REPLICATION, simulate_jobs
from .calibrate import TRAINING_END, features, predict
from .daily import load_daily
from .matrix import SPECS
from .metrics import bucket_table, by_period, summarize
from .orb import BASE_COSTS, PAPER_COSTS, Costs, OrbSpec, Sizing, run_portfolio

DATA = ad.DATA_DIR
OUT = DATA.parent / "orb_results.json"
SIZING = Sizing(mode="paper")
PROD_SIZING = Sizing(mode="production")


def sessions() -> pd.DatetimeIndex:
    d = load_daily()
    return pd.DatetimeIndex(sorted(d[d.symbol == "SPY"].date.unique()))


SESS = None


def sess() -> pd.DatetimeIndex:
    global SESS
    if SESS is None:
        SESS = sessions()
    return SESS


def _model() -> np.ndarray:
    model = json.loads((DATA / "calibration_model.json").read_text())
    if (not model.get('training_end') or not model.get('max_sample_date')
            or not pd.Timestamp(model['max_sample_date']) <= pd.Timestamp(model['training_end']) <= TRAINING_END):
        raise ValueError('calibration provenance missing or includes validation data; refit on development data only')
    return np.array(model["weights"])


def _calibrated(pess: pd.DataFrame, opt: pd.DataFrame, w: np.ndarray) -> pd.DataFrame:
    """Expected outcome of the entry-bar ambiguity: p*stopped + (1-p)*continued, p from tick-calibrated logistic."""
    key = ["date", "symbol"]
    m = pess.merge(opt[key + ["pnl_ps", "r", "exit", "reason", "exit_minute"]], on=key, suffixes=("", "_opt"))
    amb = m.ambiguous_entry_bar.to_numpy()
    p = np.zeros(len(m))
    if amb.any():
        X = np.array([features(int(r.direction), float(r.level), float(r.stop_dist), r.bar_o, r.bar_h, r.bar_l, r.bar_c)
                      for r in m[amb].itertuples()])
        p[amb] = predict(w, X)
    m["p_stopped_in_bar"] = p
    m["pnl_ps"] = p * m.pnl_ps + (1 - p) * m.pnl_ps_opt
    m["r"] = p * m.r + (1 - p) * m.r_opt
    return m


COSTS = {"paper": PAPER_COSTS, "base": BASE_COSTS, "2x": replace(BASE_COSTS, mult=2.0), "3x": replace(BASE_COSTS, mult=3.0)}


def _sel(name: str) -> str:
    from .matrix import SELECTION_SPECS
    return name if name in SELECTION_SPECS else "B0"


def sim_many(requests: list[tuple[str, str]], modes=("pessimistic", "optimistic")) -> dict[tuple[str, str, str], pd.DataFrame]:
    """requests: (spec name, cost key). One pass over the data for every (spec, cost, entry-bar mode) combination."""
    jobs = {}
    for name, ck in requests:
        for m in modes:
            jobs[f"{name}|{ck}|{m}"] = (replace(SPECS[name], same_bar=m), COSTS[ck], _sel(name))
    out = simulate_jobs(jobs)
    return {tuple(k.split("|")): v for k, v in out.items()}


def calibrated_from(sims: dict, name: str, ck: str) -> pd.DataFrame:
    return _calibrated(sims[(name, ck, "pessimistic")], sims[(name, ck, "optimistic")], _model())


def run(trades: pd.DataFrame, lo, hi, sizing: Sizing = SIZING):
    t = trades[(trades.date >= lo) & (trades.date <= hi)]
    rows, curve = run_portfolio(t, sizing)
    s = sess()
    summary = summarize(curve, rows, s[(s >= lo) & (s <= hi)], sizing.start_equity)
    return rows, curve, summary


def periods(trades: pd.DataFrame, sizing: Sizing = SIZING) -> dict:
    last = trades.date.max()
    out = {}
    for label, lo, hi in (("R", REPLICATION[0], REPLICATION[1]), ("H", HOLDOUT_START, last), ("ALL", REPLICATION[0], last)):
        rows, curve, s = run(trades, lo, hi, sizing)
        out[label] = s
    return out


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if (o != o) else float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, pd.Timestamp):
        return o.isoformat()
    return o


def save(key: str, value) -> None:
    cur = json.loads(OUT.read_text()) if OUT.exists() else {}
    cur[key] = _clean(value)
    OUT.write_text(json.dumps(cur, indent=1))


def bootstrap_expectancy(trades: pd.DataFrame, lo, hi, n: int = 2000, seed: int = 11) -> dict:
    t = trades[(trades.date >= lo) & (trades.date <= hi)]
    by_day = t.groupby("date").r.agg(["sum", "count"])
    rng = np.random.default_rng(seed)
    idx = np.arange(len(by_day))
    est = []
    for _ in range(n):
        s = rng.choice(idx, len(idx))
        est.append(by_day["sum"].to_numpy()[s].sum() / by_day["count"].to_numpy()[s].sum())
    return {"mean": float(t.r.mean()), "lo95": float(np.percentile(est, 2.5)), "hi95": float(np.percentile(est, 97.5)),
            "trades": int(len(t))}


def baseline() -> None:
    reqs = [("B0", ck) for ck in COSTS]
    sims = sim_many(reqs)
    res = {}
    for ck in COSTS:
        for mode in ("pessimistic", "calibrated", "optimistic"):
            t = calibrated_from(sims, "B0", ck) if mode == "calibrated" else sims[("B0", ck, mode)]
            res[f"{mode}/{ck}"] = periods(t)
            if mode == "calibrated" and ck == "base":
                t.to_parquet(DATA / "b0_calibrated_base.parquet")
                res["bootstrap_R"] = bootstrap_expectancy(t, *REPLICATION)
                res["bootstrap_H"] = bootstrap_expectancy(t, HOLDOUT_START, t.date.max())
                res["production_sizing"] = periods(t, PROD_SIZING)
    save("baseline", res)
    print(json.dumps(_clean(res["calibrated/base"]), indent=1))


def matrix() -> None:
    names = [n for n in SPECS if n != "E9_optimistic"]
    sims = sim_many([(n, ck) for n in names for ck in ("base", "2x")])
    res = {}
    for name in names:
        t = calibrated_from(sims, name, "base")
        r = periods(t)
        r2 = periods(calibrated_from(sims, name, "2x"))
        top10 = t.groupby("symbol").r.sum().sort_values(ascending=False).head(10).sum() / max(t.r.sum(), 1e-9)
        rows, curve, _ = run(t, REPLICATION[0], REPLICATION[1])
        yrs = {}
        if len(curve):
            cy = curve.assign(year=pd.to_datetime(curve.date).dt.year).groupby("year").pnl.sum()
            yrs = {int(k): float(v) for k, v in cy.items()}
        res[name] = {"base": r, "cost2x": r2, "top10_share_r": float(top10),
                     "r_years_positive": int(sum(v > 0 for v in yrs.values())), "r_year_pnl": yrs}
        print(name, {k: (round(v.get("sharpe", float("nan")), 2), round(v.get("expectancy_r", float("nan")), 3), v.get("trades"))
                     for k, v in r.items()}, flush=True)
    save("matrix", res)


def segments() -> None:
    t = pd.read_parquet(DATA / "b0_calibrated_base.parquet")
    res = {}
    last = t.date.max()
    spy = load_daily()
    spy = spy[spy.symbol == "SPY"].set_index("date").c
    ma200 = spy.rolling(200).mean()
    regime = (spy > ma200).map({True: "spy_above_200d", False: "spy_below_200d"})
    vol = spy.pct_change().rolling(21).std() * np.sqrt(252)
    vq = pd.qcut(vol.dropna(), 3, labels=["low_vol", "mid_vol", "high_vol"])
    t = t.assign(regime=t.date.map(regime), volreg=t.date.map(vq), year=t.date.dt.year,
                 atr_pct=t.atr14 / t.prev_close, dollar_vol=t.prev_close * t.or_vol,
                 stop_cents=t.stop_dist * 100, entry_hour=(t.entry_minute // 30) * 30)
    t["rvol_bucket"] = pd.cut(t.rvol, [0, 2, 5, 10, 30, 1e9], labels=["1-2", "2-5", "5-10", "10-30", ">30"])
    t["atrpct_bucket"] = pd.cut(t.atr_pct, [0, .015, .025, .04, 1], labels=["<1.5%", "1.5-2.5%", "2.5-4%", ">4%"])
    t["stopc_bucket"] = pd.cut(t.stop_cents, [0, 5, 10, 20, 50, 1e9], labels=["<5c", "5-10c", "10-20c", "20-50c", ">50c"])
    t["dv_bucket"] = pd.qcut(t.prev_close * t.adv14, 4, labels=["q1_least_liquid", "q2", "q3", "q4_most_liquid"])
    for period, (lo, hi) in {"R": REPLICATION, "H": (HOLDOUT_START, last)}.items():
        p = t[(t.date >= lo) & (t.date <= hi)]
        seg = {}
        for col in ("direction", "etf_like", "regime", "volreg", "rvol_bucket", "atrpct_bucket", "stopc_bucket", "entry_hour",
                    "year", "reason", "dv_bucket"):
            g = p.groupby(col, observed=True)
            seg[col] = {str(k): {"n": int(len(v)), "exp_r": float(v.r.mean()), "win": float((v.r > 0).mean()),
                                  "sum_r": float(v.r.sum())} for k, v in g}
        # extreme-RVOL artefact check: is the >30x bucket explained by a tiny denominator?
        ex = p[p.rvol > 30]
        low_den = ex[(ex.or_vol / ex.rvol) < (ex.or_vol / ex.rvol).median()]
        seg["extreme_rvol"] = {"n": int(len(ex)), "exp_r": float(ex.r.mean()) if len(ex) else None,
                               "low_denominator_half_exp_r": float(low_den.r.mean()) if len(low_den) else None,
                               "high_denominator_half_exp_r": float(ex.drop(low_den.index).r.mean()) if len(ex) > len(low_den) else None}
        # cost sensitivity by stop size (R lost to costs per trade = costs/stop_dist)
        seg["cost_r_by_stop"] = {str(k): float(((2 * (BASE_COSTS.commission + BASE_COSTS.slippage)) / v.stop_dist).mean())
                                 for k, v in p.groupby("stopc_bucket", observed=True)}
        seg["top_symbols"] = p.groupby("symbol").r.sum().sort_values(ascending=False).head(10).round(1).to_dict()
        seg["worst_symbols"] = p.groupby("symbol").r.sum().sort_values().head(10).round(1).to_dict()
        res[period] = seg
    # year and month tables
    rows, curve, _ = run(t, REPLICATION[0], last)
    res["year_return"] = {int(k.year): float(v) for k, v in by_period(curve, "Y", sess()).ret.items()}
    res["month_return"] = {str(k): float(v) for k, v in by_period(curve, "M", sess()).ret.items()}
    save("segments", res)
    print(json.dumps(_clean({k: res[k] for k in ("year_return",)}), indent=1))


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    {"baseline": baseline, "matrix": matrix, "segments": segments}.get(stage, lambda: [f() for f in (baseline, segments, matrix)])()
