"""Alternative-strategy sanity checks on the free-data history. Output: runtime/research/alt_results.json."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from . import alpaca_data as ad
from .alt import long_short_deciles, month_ends, split_adjusted_wide, stats
from .daily import load_daily

DATA = ad.DATA_DIR
NY = "America/New_York"


def stock_wide():
    daily = load_daily()
    splits = pd.read_parquet(DATA / "splits.parquet")
    adj, dvol = split_adjusted_wide(daily, splits)
    return adj, dvol


def _universe(adj, dvol, top=1000, min_price=5.0, min_dv=10e6):
    adv = dvol.rolling(63, min_periods=40).mean()
    price_ok = adj.shift(0) > min_price
    rank = adv.rank(axis=1, ascending=False)
    return (rank <= top) & (adv >= min_dv) & price_ok


def cross_sectional(adj, dvol, kind: str) -> dict:
    uni = _universe(adj, dvol)
    out = {}
    if kind == "momentum":
        rebal = month_ends(adj.index)
        rebal = rebal[rebal >= adj.index[260]]
        signal = adj.shift(21) / adj.shift(252) - 1                     # 12-1 momentum
        fwd = adj.reindex(rebal).pct_change().shift(-1)                  # return to next month end
        per_year = 12
        costs = (10.0, 30.0)
    else:                                                               # one-week reversal, weekly
        weekly = adj.groupby(adj.index.to_period("W")).tail(1)
        rebal = weekly.index[weekly.index >= adj.index[30]]
        signal = -(adj / adj.shift(5) - 1)                               # losers score high
        fwd = adj.reindex(rebal).pct_change().shift(-1)
        per_year = 52
        costs = (10.0, 30.0)
    sig = signal.reindex(rebal)
    u = uni.reindex(rebal)
    for bps in costs:
        ls = long_short_deciles(sig, fwd, u, rebal, cost_bps=bps)
        lo = long_short_deciles(sig, fwd, u, rebal, cost_bps=bps, long_only=True)
        out[f"ls_{int(bps)}bps"] = stats(ls.net, per_year) | {"gross_sharpe": stats(ls.gross, per_year).get("sharpe"),
                                                              "avg_turnover": float(ls.turnover.mean())}
        out[f"long_only_{int(bps)}bps"] = stats(lo.net, per_year) | {"excess_vs_universe_ann": float((lo.net - lo.bench).mean() * per_year),
                                                                       "avg_turnover": float(lo.turnover.mean())}
    # subperiods (first half / second half) for stability
    ls = long_short_deciles(sig, fwd, u, rebal, cost_bps=costs[0])
    mid = ls.index[len(ls) // 2]
    out["ls_first_half"] = stats(ls.net[ls.index < mid], per_year)
    out["ls_second_half"] = stats(ls.net[ls.index >= mid], per_year)
    return out


def etf_trend(lookback_months: int = 12, vol_target: float = 0.10) -> dict:
    d = pd.read_parquet(DATA / "alt_etf_daily.parquet")
    d["date"] = d.t.dt.tz_convert(NY).dt.tz_localize(None).dt.normalize()
    px = d.pivot(index="date", columns="symbol", values="c")
    core = ["SPY", "EFA", "EEM", "TLT", "IEF", "LQD", "HYG", "GLD", "DBC", "VNQ", "UUP", "IWM"]
    core = [c for c in core if c in px.columns]
    rets = px[core].pct_change()
    me = month_ends(px.index)
    mom = px[core].reindex(me).pct_change(lookback_months)
    vol = rets.rolling(63).std().reindex(me) * np.sqrt(252)
    w_sig = np.sign(mom)
    # inverse-vol, each sleeve scaled to an equal risk share, whole portfolio vol-targeted (capped at 1.5x)
    w = (w_sig / vol).div(len(core))
    nxt = px[core].reindex(me).pct_change().shift(-1)
    gross = (w.shift(0) * nxt).sum(axis=1, min_count=1) * vol_target
    port = gross.dropna()
    turn = (w.diff().abs().sum(axis=1) * vol_target).reindex(port.index).fillna(0.0)
    net = port - turn * 5.0 / 1e4
    long_only = ((w_sig.clip(lower=0) / vol).div(len(core)) * nxt).sum(axis=1, min_count=1).dropna() * vol_target
    spy = px["SPY"].reindex(me).pct_change().shift(-1).reindex(port.index)
    res = {"ls_trend": stats(net, 12), "long_only_trend": stats(long_only, 12), "spy_buyhold": stats(spy, 12),
           "corr_to_spy": float(net.corr(spy)), "assets": core}
    # first/second half
    mid = net.index[len(net) // 2]
    res["first_half"] = stats(net[net.index < mid], 12)
    res["second_half"] = stats(net[net.index >= mid], 12)
    return res


def spy_vol_managed() -> dict:
    d = pd.read_parquet(DATA / "alt_etf_daily.parquet")
    d["date"] = d.t.dt.tz_convert(NY).dt.tz_localize(None).dt.normalize()
    spy = d[d.symbol == "SPY"].set_index("date").c
    r = spy.pct_change().dropna()
    rv = r.rolling(21).std() * np.sqrt(252)
    w = (0.15 / rv).clip(upper=1.5).shift(1)
    vm = (w * r).dropna()
    return {"buyhold": stats(r.loc[vm.index], 252), "vol_managed_15pct": stats(vm, 252),
            "avg_weight": float(w.dropna().mean())}


def spy_intraday_momentum() -> dict:
    """Gao et al. style: sign of the first-30-minute return predicts the last 30 minutes (SPY). Costs 1bp per trade."""
    parts = [pd.read_parquet(p) for p in sorted((DATA / "spy_1m").glob("*.parquet"))]
    m = pd.concat(parts, ignore_index=True)
    et = m.t.dt.tz_convert(NY)
    m["date"] = et.dt.tz_localize(None).dt.normalize()
    m["min"] = et.dt.hour * 60 + et.dt.minute
    rth = m[(m["min"] >= 570) & (m["min"] < 960)]
    g = rth.groupby("date")
    first = g.apply(lambda x: x[x["min"] == 570].o.iloc[0] if (x["min"] == 570).any() else np.nan)
    f30 = g.apply(lambda x: x[x["min"] == 599].c.iloc[0] if (x["min"] == 599).any() else np.nan)
    l30o = g.apply(lambda x: x[x["min"] == 930].o.iloc[0] if (x["min"] == 930).any() else np.nan)
    last = g.apply(lambda x: x[x["min"] == 959].c.iloc[0] if (x["min"] == 959).any() else np.nan)
    first_ret, last_ret = f30 / first - 1, last / l30o - 1
    df = pd.DataFrame({"first": first_ret, "last": last_ret}).dropna()
    pos = np.sign(df["first"])
    gross = pos * df["last"]
    net = gross - 2 * 1e-4                                             # 1 bp each way
    return {"sessions": int(len(df)), "corr_first_last": float(df["first"].corr(df["last"])),
            "gross": stats(gross, 252), "net_1bp": stats(net, 252),
            "hit_rate": float((gross > 0).mean()),
            "net_by_year": {int(y): float(v) for y, v in net.groupby(net.index.year).sum().items()}}


def main() -> None:
    res = {}
    adj, dvol = stock_wide()
    res["xs_momentum_12_1"] = cross_sectional(adj, dvol, "momentum")
    res["short_term_reversal_1w"] = cross_sectional(adj, dvol, "reversal")
    res["etf_trend"] = etf_trend()
    res["spy_vol_managed"] = spy_vol_managed()
    res["spy_intraday_momentum"] = spy_intraday_momentum()
    (DATA.parent / "alt_results.json").write_text(json.dumps(res, indent=2, default=float))
    print(json.dumps(res, indent=2, default=float))


if __name__ == "__main__":
    main()
