"""Performance metrics from a daily equity curve and a trade table."""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def _full_calendar_returns(curve: pd.DataFrame, sessions: pd.DatetimeIndex | None) -> pd.Series:
    r = curve.set_index("date")["ret"]
    if sessions is not None:
        r = r.reindex(sessions, fill_value=0.0)      # no-trade sessions are zero-return days, not missing days
    return r


def summarize(curve: pd.DataFrame, trades: pd.DataFrame, sessions: pd.DatetimeIndex | None = None,
              start_equity: float = 25_000.0) -> dict:
    if curve.empty:
        return {"trades": 0}
    r = _full_calendar_returns(curve, sessions)
    eq = (1 + r).cumprod() * start_equity
    years = max((r.index[-1] - r.index[0]).days / 365.25, 1e-9)
    total = eq.iloc[-1] / start_equity - 1
    cagr = (eq.iloc[-1] / start_equity) ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1.0
    dd = (eq / eq.cummax() - 1)
    vol = r.std(ddof=1) * np.sqrt(TRADING_DAYS)
    downside = r[r < 0].std(ddof=1) * np.sqrt(TRADING_DAYS) if (r < 0).sum() > 1 else np.nan
    sharpe = r.mean() * TRADING_DAYS / vol if vol > 0 else np.nan
    sortino = r.mean() * TRADING_DAYS / downside if downside and downside > 0 else np.nan
    mdd = dd.min()
    ex = trades[trades.shares > 0] if "shares" in trades else trades
    wins, losses = ex[ex.pnl > 0], ex[ex.pnl < 0]
    gross_win, gross_loss = wins.pnl.sum(), -losses.pnl.sum()
    return {
        "trades": int(len(ex)), "sessions": int(len(r)), "years": round(years, 2),
        "total_return": total, "cagr": cagr, "sharpe": sharpe, "sortino": sortino, "max_drawdown": mdd,
        "calmar": cagr / abs(mdd) if mdd < 0 else np.nan, "ann_vol": vol,
        "win_rate": len(wins) / len(ex) if len(ex) else np.nan,
        "avg_win_r": wins.r.mean() if len(wins) else np.nan, "avg_loss_r": losses.r.mean() if len(losses) else np.nan,
        "payoff": (wins.r.mean() / -losses.r.mean()) if len(wins) and len(losses) else np.nan,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else np.nan,
        "expectancy_r": ex.r.mean() if len(ex) else np.nan,
        "expectancy_usd": ex.pnl.mean() if len(ex) else np.nan,
        "avg_gross_leverage": float((curve.gross / curve.equity_before).mean()),
        "turnover_per_year": float((2 * curve.gross / curve.equity_before).sum() / years),
        "exposure_days": float((curve.n > 0).mean()),
        "end_equity": float(eq.iloc[-1]),
    }


def by_period(curve: pd.DataFrame, freq: str, sessions: pd.DatetimeIndex | None = None) -> pd.DataFrame:
    r = _full_calendar_returns(curve, sessions)
    g = r.groupby(r.index.to_period(freq))
    return pd.DataFrame({"ret": g.apply(lambda s: (1 + s).prod() - 1), "days": g.size()})


def bucket_table(trades: pd.DataFrame, col: str, bins=None, labels=None) -> pd.DataFrame:
    t = trades[trades.shares > 0] if "shares" in trades else trades
    key = pd.cut(t[col], bins=bins, labels=labels) if bins is not None else t[col]
    g = t.groupby(key, observed=True)
    return pd.DataFrame({"n": g.size(), "exp_r": g.r.mean(), "win": g.r.apply(lambda s: (s > 0).mean()),
                         "sum_usd": g.pnl.sum()})
