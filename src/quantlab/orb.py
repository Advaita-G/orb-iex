"""Opening-range-breakout research engine (stocks in play). Pure functions; no network, no orders.

Reference: Zarattini, Barbon, Aziz (2024), "A Profitable Day Trading Strategy For The U.S. Equity Market".
Published rules reproduced here (see docs/ORB-RESEARCH-2026-10-05.md for the full comparison):
  eligible: opening price > $5, 14-day average volume >= 1M, 14-day ATR > $0.50
  stock in play: opening-range volume / mean of prior 14 opening-range volumes >= 1, top 20 by that ratio
  entry: long if the first 5-minute candle closed up (stop-buy at its high), short if down (stop-sell at its low)
  stop: 10% of the 14-day ATR from the executed entry price; flat at the close
  sizing: loss at the stop = 1% of capital, gross leverage <= 4x; commission $0.0035 per share
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import time as dtime

import numpy as np
import pandas as pd

NY = "America/New_York"
OPEN_MIN = 9 * 60 + 30
CLOSE_MIN = 16 * 60


@dataclass(frozen=True)
class OrbSpec:
    or_minutes: int = 5
    top_n: int = 20
    min_price: float = 5.0
    min_adv: float = 1_000_000.0
    min_atr: float = 0.50                  # absolute ATR gate ($)
    min_atr_pct: float | None = None       # percentage ATR gate (replaces the absolute gate when set)
    min_rvol: float = 1.0
    rvol_lookback: int = 14
    min_rvol_history: int = 10             # prior opening ranges required inside the lookback
    stop_atr_frac: float = 0.10
    stop_kind: str = "atr"                 # 'atr' (distance from fill) | 'or' (opposite side of the opening range)
    cutoff_minute: int | None = None       # last minute (ET, minutes after midnight) a trigger may fire; None = all day
    target_r: float | None = None          # fixed profit target in R; None = hold to the close
    allow_long: bool = True
    allow_short: bool = True
    include: str = "all"                   # 'all' | 'stocks' | 'etf'
    same_bar: str = "pessimistic"          # entry-bar stop ambiguity: pessimistic | optimistic


@dataclass(frozen=True)
class Costs:
    commission: float = 0.0035             # $/share/side (the paper's figure)
    slippage: float = 0.01                 # $/share/side on stop-market and market fills
    mult: float = 1.0

    @property
    def side(self) -> float:
        return (self.commission + self.slippage) * self.mult


PAPER_COSTS = Costs(slippage=0.0)          # commission only: the paper's own cost assumption
BASE_COSTS = Costs()


# ------------------------------------------------------------------ opening features
def opening_ranges(bars: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Opening range per (symbol, date) from 1-minute bars: first `minutes` minutes of the regular session."""
    et = bars["t"].dt.tz_convert(NY)
    m = et.dt.hour * 60 + et.dt.minute
    sel = bars[(m >= OPEN_MIN) & (m < OPEN_MIN + minutes)].copy()
    sel["date"] = et[sel.index].dt.tz_localize(None).dt.normalize()
    sel = sel.sort_values(["symbol", "t"])
    g = sel.groupby(["symbol", "date"], sort=False)
    out = g.agg(or_open=("o", "first"), or_high=("h", "max"), or_low=("l", "min"), or_close=("c", "last"),
                or_vol=("v", "sum"), or_bars=("v", "size"), first_minute=("t", "min")).reset_index()
    return out


def add_rvol(opening: pd.DataFrame, lookback: int, min_history: int) -> pd.DataFrame:
    """Same-window relative volume against the symbol's previous `lookback` opening ranges (sessions with no
    opening range are skipped, not zero-filled). Today's volume never enters its own denominator."""
    o = opening.sort_values(["symbol", "date"]).copy()
    prior = o.groupby("symbol")["or_vol"].transform(lambda s: s.shift(1).rolling(lookback, min_periods=min_history).mean())
    # The window is "the previous N observations"; reject it when those observations are spread over far more than
    # N sessions (a symbol that was absent for weeks has no meaningful 14-day baseline).
    sessions = np.sort(o["date"].unique())
    sidx = o["date"].map(pd.Series(np.arange(len(sessions)), index=sessions))
    span = sidx - sidx.groupby(o["symbol"]).shift(min_history)
    stale = span > lookback + 6
    o["rvol_base"] = prior.where(~stale)
    o["rvol"] = o["or_vol"] / o["rvol_base"]
    return o


# ------------------------------------------------------------------ selection
def select_candidates(day_table: pd.DataFrame, spec: OrbSpec) -> pd.DataFrame:
    """day_table: opening features joined with daily features for ONE date. Returns the ranked top-N."""
    t = day_table
    atr_ok = (t.atr14 / t.prev_close >= spec.min_atr_pct) if spec.min_atr_pct is not None else (t.atr14 > spec.min_atr)
    ok = ((t.or_open > spec.min_price) & (t.adv14 >= spec.min_adv) & atr_ok & (t.rvol >= spec.min_rvol)
          & (t.or_close != t.or_open) & ~t.split_window & t.rvol.notna())
    if spec.include != "all" and "etf_like" in t:
        ok &= t.etf_like if spec.include == "etf" else ~t.etf_like
    c = t[ok].copy()
    c["direction"] = np.where(c.or_close > c.or_open, 1, -1)
    if not spec.allow_long:
        c = c[c.direction == -1]
    if not spec.allow_short:
        c = c[c.direction == 1]
    return c.sort_values(["rvol", "symbol"], ascending=[False, True]).head(spec.top_n)


# ------------------------------------------------------------------ trade simulation
def path_arrays(path: pd.DataFrame):
    """Pre-convert one symbol-day of 1-minute bars (sorted) to numpy arrays: (minute, o, h, l, c)."""
    et = path["t"].dt.tz_convert(NY)
    minute = (et.dt.hour * 60 + et.dt.minute).to_numpy()
    return minute, *(path[k].to_numpy(float) for k in ("o", "h", "l", "c"))


def simulate_trade(path: pd.DataFrame, direction: int, level: float, atr: float, or_extreme: float,
                   spec: OrbSpec, costs: Costs) -> dict | None:
    return simulate_arrays(path_arrays(path), direction, level, atr, or_extreme, spec, costs)


def simulate_arrays(arrays, direction: int, level: float, atr: float, or_extreme: float,
                    spec: OrbSpec, costs: Costs) -> dict | None:
    """One symbol-day on 1-minute bars (after the opening range). Returns None when the trigger never fires.

    Conventions (documented in the report): a stop-market order fills at the trigger price, or at the bar open
    when the bar gaps through it; a trigger fires on the first bar whose range touches the level; the stop is
    checked on later bars; the entry bar's own low/high is ambiguous (pessimistic = stopped if the bar's range
    covers the stop and it did not close safely inside, optimistic = never stopped inside the entry bar)."""
    minute, o, h, l, c = arrays
    n = len(minute)
    if n == 0:
        return None
    start = spec.or_minutes + OPEN_MIN
    valid = minute >= start
    d = direction
    trig = np.where(valid & ((h >= level) if d == 1 else (l <= level)))[0]
    if len(trig) == 0:
        return None
    j = int(trig[0])
    if spec.cutoff_minute is not None and minute[j] >= spec.cutoff_minute:
        return None
    gap_through = (o[j] > level) if d == 1 else (o[j] < level)
    raw_fill = o[j] if gap_through else level
    fill = raw_fill + d * costs.slippage * costs.mult          # entry slippage adverse
    stop_dist_plan = spec.stop_atr_frac * atr if spec.stop_kind == "atr" else abs(level - or_extreme)
    if stop_dist_plan <= 0:
        return None
    stop = fill - d * stop_dist_plan
    target = fill + d * spec.target_r * stop_dist_plan if spec.target_r else None
    exit_px, reason, k_exit, ambiguous = None, "eod", n - 1, False

    def stop_fill(k):
        px = min(o[k], stop) if d == 1 else max(o[k], stop)    # gap through the stop fills at the open
        return px - d * costs.slippage * costs.mult

    # entry bar
    hit_stop_entry = (l[j] <= stop) if d == 1 else (h[j] >= stop)
    if hit_stop_entry:
        closed_safe = (c[j] > stop) if d == 1 else (c[j] < stop)
        if not closed_safe or spec.same_bar == "pessimistic":
            ambiguous = closed_safe
            exit_px, reason, k_exit = (stop - d * costs.slippage * costs.mult), "stop_same_bar", j
    if exit_px is None and target is not None:
        if (h[j] >= target) if d == 1 else (l[j] <= target):
            if spec.same_bar != "pessimistic":
                exit_px, reason, k_exit = target - d * costs.slippage * costs.mult, "target", j
    if exit_px is None:
        for k in range(j + 1, n):
            if (o[k] <= stop if d == 1 else o[k] >= stop):
                exit_px, reason, k_exit = stop_fill(k), "stop", k
                break
            if (l[k] <= stop if d == 1 else h[k] >= stop):
                exit_px, reason, k_exit = stop_fill(k), "stop", k
                break
            if target is not None and ((h[k] >= target) if d == 1 else (l[k] <= target)):
                exit_px, reason, k_exit = target - d * costs.slippage * costs.mult, "target", k
                break
    if exit_px is None:
        exit_px = c[n - 1] - d * costs.slippage * costs.mult
    commission = 2 * costs.commission * costs.mult
    pnl_ps = d * (exit_px - fill) - commission
    # excursion in R over the holding window (MFE/MAE use bar extremes)
    seg = slice(j, k_exit + 1)
    fav = (h[seg].max() - fill) if d == 1 else (fill - l[seg].min())
    adv = (fill - l[seg].min()) if d == 1 else (h[seg].max() - fill)
    return {"entry_minute": int(minute[j]), "exit_minute": int(minute[k_exit]), "entry": fill, "exit": exit_px,
            "stop_dist": stop_dist_plan, "pnl_ps": pnl_ps, "r": pnl_ps / stop_dist_plan, "reason": reason,
            "ambiguous_entry_bar": ambiguous, "mfe_r": fav / stop_dist_plan, "mae_r": adv / stop_dist_plan,
            "gap_entry": bool(gap_through), "level": level, "j": j,
            "bar_o": o[j], "bar_h": h[j], "bar_l": l[j], "bar_c": c[j]}


# ------------------------------------------------------------------ portfolio
@dataclass(frozen=True)
class Sizing:
    mode: str = "paper"          # 'paper': risk 1% of equity per trade, gross <= 4x equity (scaled down together)
    #                              'production': risk 1% of (equity/20) per trade, each position <= 4x equity/20
    risk: float = 0.01
    leverage: float = 4.0
    slots: int = 20
    start_equity: float = 25_000.0


def size_day(trades: pd.DataFrame, equity: float, sz: Sizing) -> pd.Series:
    """Shares per trade for one day. trades needs entry, stop_dist."""
    if trades.empty:
        return pd.Series(dtype=float)
    if sz.mode == "production":
        risk_amt = equity * sz.risk / sz.slots
        raw = np.floor(risk_amt / trades.stop_dist)
        cap = np.floor(sz.leverage * equity / sz.slots / trades.entry)
        return np.minimum(raw, cap).clip(lower=0)
    raw = np.floor(equity * sz.risk / trades.stop_dist)
    gross = (raw * trades.entry).sum()
    limit = sz.leverage * equity
    if gross > limit > 0:
        raw = np.floor(raw * limit / gross)
    return raw.clip(lower=0)


def run_portfolio(trades: pd.DataFrame, sz: Sizing) -> tuple[pd.DataFrame, pd.DataFrame]:
    """trades: one row per executed trade with date, pnl_ps, entry, stop_dist. Returns (trade rows with shares/pnl,
    daily equity curve). Equity compounds; days without trades carry flat."""
    rows, curve = [], []
    equity = sz.start_equity
    for date, day in trades.sort_values(["date", "symbol"]).groupby("date", sort=True):
        shares = size_day(day, equity, sz)
        d = day.assign(shares=shares.to_numpy(), equity_before=equity)
        d["pnl"] = d.shares * d.pnl_ps
        d["notional"] = d.shares * d.entry
        pnl = float(d.pnl.sum())
        curve.append({"date": date, "equity_before": equity, "pnl": pnl, "ret": pnl / equity if equity > 0 else 0.0,
                      "gross": float(d.notional.sum()), "n": int((d.shares > 0).sum())})
        equity += pnl
        if equity <= 0:
            rows.append(d)
            break
        rows.append(d)
    if not rows:
        return pd.DataFrame(), pd.DataFrame()
    return pd.concat(rows, ignore_index=True), pd.DataFrame(curve)
