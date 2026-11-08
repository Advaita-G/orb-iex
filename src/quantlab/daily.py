"""Point-in-time daily features. Every value for date d uses sessions strictly before d."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .alpaca_data import DATA_DIR

ATR_N = 14
ADV_N = 14


def load_daily(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    parts = [pd.read_parquet(p) for p in sorted((data_dir / "daily").glob("*.parquet"))]
    df = pd.concat([p for p in parts if len(p)], ignore_index=True)
    # Alpaca stamps daily bars at 04:00/05:00 UTC: the ET calendar date is the session.
    df["date"] = df["t"].dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize()
    df = df.drop(columns=["t"]).sort_values(["symbol", "date"]).drop_duplicates(["symbol", "date"]).reset_index(drop=True)
    return df


def daily_features(df: pd.DataFrame, splits: pd.DataFrame | None) -> pd.DataFrame:
    g = df.groupby("symbol", sort=False)
    prev_close = g["c"].shift(1)
    tr = np.maximum.reduce([df.h - df.l, (df.h - prev_close).abs(), (df.l - prev_close).abs()])
    tr = pd.Series(tr, index=df.index).where(prev_close.notna())
    out = df[["symbol", "date"]].copy()
    out["prev_close"] = prev_close
    out["atr14"] = tr.groupby(df.symbol).transform(lambda s: s.rolling(ATR_N, min_periods=ATR_N).mean().shift(1))
    out["adv14"] = df.groupby("symbol")["v"].transform(lambda s: s.rolling(ADV_N, min_periods=ADV_N).mean().shift(1))
    out["split_window"] = False
    if splits is not None and len(splits):
        sp = splits.copy()
        sp["ex_date"] = pd.to_datetime(sp["ex_date"])
        by_symbol = {s: grp.index.to_numpy() for s, grp in out.groupby("symbol", sort=False)}
        dates = out["date"].to_numpy()
        flag = np.zeros(len(out), dtype=bool)
        for sym, ex in zip(sp.symbol, sp.ex_date):
            idx = by_symbol.get(sym)
            if idx is None:
                continue
            first = np.searchsorted(dates[idx], np.datetime64(ex))
            flag[idx[first:first + ATR_N + 1]] = True      # raw-price windows that straddle the split are unusable
        out["split_window"] = flag
    return out
