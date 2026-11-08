"""ORB research pipeline stages. Each stage reads/writes parquet under runtime/research/data and is resumable."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from . import alpaca_data as ad
from .daily import daily_features, load_daily
from .orb import OrbSpec, add_rvol, opening_ranges, select_candidates

DATA = ad.DATA_DIR
FIRST_SESSION = pd.Timestamp("2016-01-04")
OR_LENGTHS = (1, 5, 10, 15)
# loose pre-filter: decides which symbol-days get intraday data (the exact published filters are applied later)
LOOSE = dict(min_prev_close=3.0, min_adv=300_000.0, min_atr=0.0)


def etf_like(name: str) -> bool:
    n = (name or "").upper()
    keys = (" ETF", " ETN", " FUND", " TRUST", "ISHARES", "SPDR", "INVESCO", "PROSHARES", "VANGUARD", "DIREXION",
            "WISDOMTREE", "VANECK", "GLOBAL X", "FIRST TRUST", "SCHWAB", "ULTRA", "SELECT SECTOR", "INDEX")
    return any(k in n for k in keys)


def features() -> pd.DataFrame:
    path = DATA / "features.parquet"
    if path.exists():
        return pd.read_parquet(path)
    df = load_daily()
    splits = pd.read_parquet(DATA / "splits.parquet") if (DATA / "splits.parquet").exists() else None
    f = daily_features(df, splits)
    f = f.merge(df[["symbol", "date", "o", "h", "l", "c", "v"]], on=["symbol", "date"])
    f.to_parquet(path)
    return f


def stage_lists() -> None:
    f = features()
    keep = ((f.prev_close > LOOSE["min_prev_close"]) & (f.adv14 >= LOOSE["min_adv"]) & ~f.split_window
            & (f.date >= FIRST_SESSION))
    u = f.loc[keep, ["symbol", "date"]]
    by_day = {d.date().isoformat(): sorted(g.symbol) for d, g in u.groupby("date")}
    (DATA / "open_lists.json").write_text(json.dumps(by_day))
    n = np.array([len(v) for v in by_day.values()])
    print(f"{len(by_day)} sessions; symbols/session median {int(np.median(n))}, max {n.max()}, total rows {n.sum()}")


def stage_fetch_open(part: int = 0, parts: int = 1, reverse: bool = False) -> None:
    lists = json.loads((DATA / "open_lists.json").read_text())
    order = sorted(lists, reverse=reverse)                  # reverse workers meet the forward ones in the middle
    days = order[part::parts]                               # interleaved so parallel workers share the same eras
    ad.fetch_openings({date.fromisoformat(k): lists[k] for k in days})


def _opening_tables() -> dict[int, pd.DataFrame]:
    out: dict[int, list] = {m: [] for m in OR_LENGTHS}
    for p in sorted((DATA / "open1m").glob("*.parquet")):
        b = pd.read_parquet(p)
        if b.empty:
            continue
        for m in OR_LENGTHS:
            out[m].append(opening_ranges(b, m))
    tabs = {}
    for m, parts in out.items():
        t = pd.concat(parts, ignore_index=True)
        for c in ("or_open", "or_high", "or_low", "or_close", "or_vol"):
            t[c] = t[c].astype("float64")
        tabs[m] = t.drop(columns=["first_minute"])
    return tabs


def stage_openings() -> None:
    names = ad.assets(ad.make_client())[["symbol", "name"]]
    names["etf_like"] = names.name.map(etf_like)
    f = features()[["symbol", "date", "prev_close", "atr14", "adv14", "split_window"]]
    for m, t in _opening_tables().items():
        t = add_rvol(t, 14, 10).merge(f, on=["symbol", "date"], how="inner").merge(names[["symbol", "etf_like"]],
                                                                                 on="symbol", how="left")
        t["etf_like"] = t.etf_like.fillna(False).astype(bool)
        t.to_parquet(DATA / f"openings_{m}m.parquet")
        print(f"openings {m}m: {len(t)} rows", flush=True)


def select_all(specs: dict[str, OrbSpec]) -> dict[str, pd.DataFrame]:
    """Ranked candidates per spec, using that spec's opening-range length."""
    cache: dict[int, pd.DataFrame] = {}
    out = {}
    for name, spec in specs.items():
        if spec.or_minutes not in cache:
            cache[spec.or_minutes] = pd.read_parquet(DATA / f"openings_{spec.or_minutes}m.parquet")
        t = cache[spec.or_minutes]
        t = t[t.date >= FIRST_SESSION]
        parts = [select_candidates(g, spec).assign(date=d) for d, g in t.groupby("date", sort=True)]
        parts = [p for p in parts if len(p)]
        out[name] = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return out


if __name__ == "__main__":
    if sys.argv[1] == "fetch_open" and len(sys.argv) >= 4:
        stage_fetch_open(int(sys.argv[2]), int(sys.argv[3]), "--reverse" in sys.argv)
    else:
        {"lists": stage_lists, "fetch_open": stage_fetch_open, "openings": stage_openings}[sys.argv[1]]()
