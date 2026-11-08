"""Resolve the entry-bar ambiguity with real ticks on a sample, then model P(stopped inside the entry minute).

1-minute bars cannot say whether price tagged the stop before or after it triggered the entry. For a sample of the
ambiguous trades we fetch that single minute of SIP trades, replay it tick by tick, and fit a small logistic model on
bar-shape features. The backtest then uses expected PnL = p * (stopped) + (1 - p) * (continued)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from . import alpaca_data as ad

NY = ZoneInfo("America/New_York")
TRAINING_END = pd.Timestamp("2019-12-31")


def minute_window(day: pd.Timestamp, minute_of_day: int) -> tuple[str, str]:
    start = datetime(day.year, day.month, day.day, minute_of_day // 60, minute_of_day % 60, tzinfo=NY).astimezone(timezone.utc)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ"), (start + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_ticks(client, symbol: str, day: pd.Timestamp, minute_of_day: int) -> np.ndarray | None:
    s, e = minute_window(day, minute_of_day)
    prices, token = [], None
    while True:
        params = {"symbols": symbol, "start": s, "end": e, "feed": "sip", "limit": 10000}
        if token:
            params["page_token"] = token
        r = client.data_get("/v2/stocks/trades", params=params, purpose="research:ticks")
        if not r.ok or not isinstance(r.data, dict):
            return None
        prices += [t["p"] for t in (r.data.get("trades") or {}).get(symbol, [])]
        token = r.data.get("next_page_token")
        if not token:
            break
    return np.array(prices) if prices else None


def resolve(prices: np.ndarray, d: int, level: float, stop_dist: float, lo: float, hi: float) -> bool | None:
    """True if, after the first tick through `level`, a later tick reaches the stop within the same minute."""
    p = prices[(prices >= lo * 0.9995) & (prices <= hi * 1.0005)]            # same band as the bar
    hit = np.where(p >= level)[0] if d == 1 else np.where(p <= level)[0]
    if len(hit) == 0:
        return None
    i = hit[0]
    fill = max(level, p[i]) if d == 1 else min(level, p[i])
    stop = fill - d * stop_dist
    later = p[i + 1:]
    return bool((later <= stop).any() if d == 1 else (later >= stop).any())


def features(d, level, stop_dist, bar_o, bar_h, bar_l, bar_c) -> np.ndarray:
    rng = max(bar_h - bar_l, 1e-9)
    return np.array([1.0, min(stop_dist / rng, 3.0), d * (bar_c - level) / rng, d * (bar_o - level) / rng,
                     np.log(max(stop_dist / max(level, 1e-9), 1e-6))])


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1e-2) -> np.ndarray:
    def loss(w):
        z = X @ w
        return np.mean(np.logaddexp(0, z) - y * z) + l2 * np.sum(w[1:] ** 2)
    return minimize(loss, np.zeros(X.shape[1]), method="BFGS").x


def predict(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-(X @ w)))


def calibrate(amb: pd.DataFrame, n: int = 1200, seed: int = 7) -> dict:
    """amb: ambiguous B0 trades (date, symbol, direction, level, stop_dist, entry-bar fields, entry_minute)."""
    amb = amb[amb.date <= TRAINING_END]
    if amb.empty:
        raise ValueError('calibration requires development observations through 2019 only')
    rng = np.random.default_rng(seed)
    sample = amb.groupby(amb.date.dt.year, group_keys=False).apply(
        lambda g: g.sample(min(len(g), max(n // amb.date.dt.year.nunique(), 1)), random_state=int(rng.integers(1e9))))
    client = ad.make_client(100)
    rows = []
    for r in sample.itertuples():
        ticks = fetch_ticks(client, r.symbol, r.date, int(r.entry_minute))
        if ticks is None:
            continue
        out = resolve(ticks, int(r.direction), float(r.level), float(r.stop_dist), float(r.bar_l), float(r.bar_h))
        if out is None:
            continue
        rows.append({"date": r.date, "symbol": r.symbol, "stopped": int(out),
                     "x": features(int(r.direction), float(r.level), float(r.stop_dist), r.bar_o, r.bar_h, r.bar_l, r.bar_c).tolist()})
    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError('calibration has no resolved development ticks')
    X = np.array(df.x.tolist())
    y = df.stopped.to_numpy(float)
    years = df.date.dt.year.to_numpy()
    p_cv = np.full(len(y), np.nan)
    p_const = np.full(len(y), np.nan)
    for year in sorted(set(years))[1:]:
        w = fit_logistic(X[years < year], y[years < year])
        p_cv[years == year] = predict(w, X[years == year])
        p_const[years == year] = y[years < year].mean()
    checked = np.isfinite(p_cv)
    base = y.mean()
    def ll(p):
        return float(-np.mean(y[checked] * np.log(np.clip(p[checked], 1e-6, 1)) +
                             (1 - y[checked]) * np.log(np.clip(1 - p[checked], 1e-6, 1)))) if checked.any() else None
    w = fit_logistic(X, y)
    res = {"n": int(len(y)), "stopped_share": float(base), "logloss_const": ll(p_const),
           "logloss_cv": ll(p_cv),
           "cv_method": "expanding_year", "cv_n": int(checked.sum()),
           "training_end": TRAINING_END.date().isoformat(), "max_sample_date": df.date.max().date().isoformat(),
           "weights": w.tolist(),
           "by_year": {int(k): float(v) for k, v in df.groupby(df.date.dt.year).stopped.mean().items()}}
    df.drop(columns="x").to_parquet(ad.DATA_DIR / "calibration_sample.parquet")
    (ad.DATA_DIR / "calibration_model.json").write_text(json.dumps(res, indent=2))
    return res
