"""Quick, honest checks of alternative strategies on the same free Alpaca data (2015-09 → 2026-10).

These are sanity replications with known data limits (split-adjusted but not dividend-adjusted stock prices, current
asset list = survivorship bias). They complement, not replace, the published evidence cited in the report.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .alpaca_data import DATA_DIR


def split_adjusted_wide(daily: pd.DataFrame, splits: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(adjusted close, dollar volume) as dates x symbols. Forward split new/old = r divides earlier prices by r."""
    close = daily.pivot(index="date", columns="symbol", values="c")
    dvol = (daily.assign(dv=daily.c * daily.v)).pivot(index="date", columns="symbol", values="dv")
    adj = close.copy()
    if splits is not None and len(splits):
        for sym, ex, new, old in zip(splits.symbol, pd.to_datetime(splits.ex_date), splits.new_rate, splits.old_rate):
            if sym in adj.columns and old and new:
                adj.loc[adj.index < ex, sym] = adj.loc[adj.index < ex, sym] / (new / old)
    return adj, dvol


def month_ends(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    s = pd.Series(index, index=index)
    return pd.DatetimeIndex(s.groupby([index.year, index.month]).max().values)


def long_short_deciles(signal: pd.DataFrame, fwd: pd.DataFrame, universe: pd.DataFrame, rebal: pd.DatetimeIndex,
                       n_bins: int = 10, cost_bps: float = 10.0, long_only: bool = False) -> pd.DataFrame:
    """Equal-weight top/bottom bin, rebalanced on `rebal` dates, held until the next rebalance date.
    `fwd` is the return from each rebalance date to the next one. Costs: cost_bps per side on traded weight."""
    prev_w = pd.Series(dtype=float)
    rows = []
    for d in rebal[:-1]:
        s = signal.loc[d].where(universe.loc[d]).dropna()
        if len(s) < n_bins * 5:
            continue
        q = pd.qcut(s.rank(method="first"), n_bins, labels=False)
        long = s.index[q == n_bins - 1]
        short = s.index[q == 0]
        w = pd.Series(0.0, index=s.index)
        w[long] = 1.0 / len(long)
        if not long_only:
            w[short] = -1.0 / len(short)
        r = fwd.loc[d]
        gross = float((w * r.reindex(w.index).fillna(0.0)).sum())
        allw = w.reindex(prev_w.index.union(w.index)).fillna(0.0)
        turnover = float((allw - prev_w.reindex(allw.index).fillna(0.0)).abs().sum())
        net = gross - turnover * cost_bps / 1e4
        base = float(fwd.loc[d].reindex(s.index).mean()) if long_only else 0.0
        rows.append({"date": d, "gross": gross, "net": net, "turnover": turnover, "n_long": len(long),
                     "bench": base})
        prev_w = w
    return pd.DataFrame(rows).set_index("date")


def stats(r: pd.Series, periods_per_year: float) -> dict:
    r = r.dropna()
    if len(r) < 3:
        return {}
    eq = (1 + r).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    sharpe = r.mean() / r.std(ddof=1) * np.sqrt(periods_per_year) if r.std(ddof=1) > 0 else np.nan
    years = len(r) / periods_per_year
    cagr = eq.iloc[-1] ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1
    return {"periods": len(r), "ann_return": r.mean() * periods_per_year, "ann_vol": r.std(ddof=1) * np.sqrt(periods_per_year),
            "sharpe": sharpe, "cagr": cagr, "max_dd": dd}
