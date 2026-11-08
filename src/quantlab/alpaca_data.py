"""Resumable historical-data download (GET only; free Alpaca plan; SIP history older than 15 minutes).

Everything lands in runtime/research/data as parquet shards so a restart continues where it stopped.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pandas as pd

from orb_iex.http import ReadOnlyClient
from orb_iex.secrets import load_credentials, load_env_file

DATA_DIR = Path("runtime/research/data")
SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX"}          # same listing venues as production (P-1)
HISTORY_START = "2015-09-01"
HISTORY_END = "2026-10-02"


def make_client(per_minute: int = 150) -> ReadOnlyClient:
    load_env_file(".env")
    return ReadOnlyClient(load_credentials(), per_minute, per_minute, timeout_seconds=60, max_attempts=5)


def assets(client: ReadOnlyClient, refresh: bool = False) -> pd.DataFrame:
    path = DATA_DIR / "assets.parquet"
    if path.exists() and not refresh:
        return pd.read_parquet(path)
    frames = []
    for status in ("active", "inactive"):
        r = client.trading_get("/v2/assets", params={"status": status, "asset_class": "us_equity"}, purpose="research:assets")
        if not r.ok or not isinstance(r.data, list):
            raise ConnectionError(f"assets {status}: HTTP {r.status}")
        frames.append(pd.DataFrame(r.data))
    df = pd.concat(frames, ignore_index=True)
    keep = ["symbol", "name", "exchange", "status", "tradable", "shortable", "easy_to_borrow", "fractionable"]
    df = df[[c for c in keep if c in df.columns]]
    df = df[df.exchange.isin(EXCHANGES) & df.symbol.map(lambda s: bool(SYMBOL_RE.match(s)))]
    df = df.drop_duplicates("symbol").sort_values("symbol").reset_index(drop=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    return df


def _get_with_patience(client: ReadOnlyClient, path: str, params: dict, purpose: str, tries: int = 12):
    """The client already retries 429/5xx a few times; a long download also outlasts rate-limit bursts, so wait and
    try again instead of dying (downloads are idempotent GETs)."""
    for attempt in range(tries):
        try:
            r = client.data_get(path, params=params, purpose=purpose)
        except ConnectionError:
            r = None
        if r is not None and r.status != 429 and r.status < 500:
            return r
        time.sleep(min(10 * (attempt + 1), 90))
    raise ConnectionError(f"{path} still failing after {tries} patient retries")


def bars(client: ReadOnlyClient, symbols: list[str], timeframe: str, start: str, end: str,
         adjustment: str = "raw", feed: str = "sip") -> pd.DataFrame:
    """All bars for the symbols, following pagination. Invalid symbols are dropped (one bad name fails a whole request)."""
    rows: list[dict] = []
    symbols = list(symbols)
    token = None
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start, "end": end, "feed": feed,
                  "adjustment": adjustment, "limit": 10000}
        if token:
            params["page_token"] = token
        r = _get_with_patience(client, "/v2/stocks/bars", params, "research:bars")
        if r.status == 400 and "invalid symbol" in r.text:
            bad = re.search(r"invalid symbol: ([^\"]+)", r.text).group(1).strip()
            symbols = [s for s in symbols if s != bad]
            token = None
            rows = []
            continue
        if not r.ok or not isinstance(r.data, dict):
            raise ConnectionError(f"bars HTTP {r.status}: {r.text[:200]}")
        for sym, items in (r.data.get("bars") or {}).items():
            for b in items or []:
                rows.append({"symbol": sym, "t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
                             "v": b["v"], "n": b.get("n"), "vw": b.get("vw")})
        token = r.data.get("next_page_token")
        if not token:
            break
    if not rows:
        return pd.DataFrame(columns=["symbol", "t", "o", "h", "l", "c", "v", "n", "vw"])
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"], utc=True)
    return df


def fetch_daily(chunk: int = 150, reverse: bool = False) -> None:
    client = make_client()
    a = assets(client)
    syms = list(a.symbol)
    out = DATA_DIR / "daily"
    out.mkdir(parents=True, exist_ok=True)
    starts = list(range(0, len(syms), chunk))
    for i in (reversed(starts) if reverse else starts):
        shard = out / f"{i // chunk:04d}.parquet"
        if shard.exists():
            continue
        df = bars(client, syms[i:i + chunk], "1Day", HISTORY_START, HISTORY_END + "T23:59:59Z")
        df.to_parquet(shard)
        print(f"daily shard {shard.name}: {len(df)} rows, {df.symbol.nunique()} symbols", flush=True)


def fetch_splits(chunk: int = 200) -> None:
    client = make_client()
    syms = list(assets(client).symbol)
    rows = []
    for i in range(0, len(syms), chunk):
        token = None
        while True:
            params = {"symbols": ",".join(syms[i:i + chunk]), "types": "forward_split,reverse_split",
                      "start": HISTORY_START, "end": HISTORY_END, "limit": 1000}
            if token:
                params["page_token"] = token
            r = client.data_get("/v1/corporate-actions", params=params, purpose="research:corp")
            if r.status == 400 and "invalid symbol" in r.text:
                bad = re.search(r"invalid symbol: ([^\"]+)", r.text).group(1).strip()
                syms = [s for s in syms if s != bad]
                continue
            if not r.ok:
                raise ConnectionError(f"corp actions HTTP {r.status}: {r.text[:200]}")
            ca = r.data.get("corporate_actions") or {}
            for kind in ("forward_splits", "reverse_splits"):
                for x in ca.get(kind, []):
                    rows.append({"symbol": x["symbol"], "ex_date": x["ex_date"], "new_rate": x["new_rate"],
                                 "old_rate": x["old_rate"], "kind": kind})
            token = r.data.get("next_page_token")
            if not token:
                break
        print(f"splits chunk {i // chunk}: total {len(rows)}", flush=True)
    pd.DataFrame(rows).to_parquet(DATA_DIR / "splits.parquet")




# ---- intraday windows -----------------------------------------------------------------------------

from datetime import date as _date, datetime as _dt, time as _time  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

NY = ZoneInfo("America/New_York")


def _utc(day: _date, hh: int, mm: int) -> str:
    return _dt.combine(day, _time(hh, mm), tzinfo=NY).astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_window(client: ReadOnlyClient, symbols: list[str], day: _date, start_hm: tuple[int, int],
                 end_hm: tuple[int, int], chunk: int = 800, feed: str = "sip") -> pd.DataFrame:
    """1-minute SIP bars [start, end) in ET for one session. Chunked: ~800 symbols per request keeps URLs short."""
    frames = []
    for i in range(0, len(symbols), chunk):
        df = bars(client, symbols[i:i + chunk], "1Min", _utc(day, *start_hm), _utc(day, *end_hm), feed=feed)
        if len(df):
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["symbol", "t", "o", "h", "l", "c", "v", "n", "vw"])
    out = pd.concat(frames, ignore_index=True)
    end_utc = pd.Timestamp(_utc(day, *end_hm))
    return out[out.t < end_utc].reset_index(drop=True)


def fetch_openings(symbols_by_day: dict, out_dir: Path | None = None, minutes: int = 15) -> None:
    """Resumable: one parquet per session with the first `minutes` 1-minute bars of every listed symbol."""
    client = make_client()
    out_dir = out_dir or DATA_DIR / "open1m"
    out_dir.mkdir(parents=True, exist_ok=True)
    for day, syms in symbols_by_day.items():       # caller controls the order
        path = out_dir / f"{day.isoformat()}.parquet"
        if path.exists():
            continue
        df = fetch_window(client, syms, day, (9, 30), (9, 30 + minutes))
        df.to_parquet(path)
        print(f"open {day}: {len(syms)} requested, {df.symbol.nunique()} with bars, {len(df)} rows", flush=True)


def fetch_openings_iex(symbols_by_day: dict, minutes: int = 15) -> None:
    """IEX-only opening bars for recent sessions: quantifies how the production (IEX) opening data differs from SIP."""
    fetch_openings_feed(symbols_by_day, "iex", DATA_DIR / "open1m_iex", minutes)


def fetch_openings_feed(symbols_by_day: dict, feed: str, out_dir: Path, minutes: int = 15) -> None:
    client = make_client()
    out_dir.mkdir(parents=True, exist_ok=True)
    for day, syms in symbols_by_day.items():
        path = out_dir / f"{day.isoformat()}.parquet"
        if path.exists():
            continue
        df = fetch_window(client, syms, day, (9, 30), (9, 30 + minutes), feed=feed)
        df.to_parquet(path)
        print(f"{feed} open {day}: {len(syms)} requested, {df.symbol.nunique()} with bars", flush=True)


def fetch_paths(symbols_by_day: dict, out_dir: Path | None = None) -> None:
    """Resumable: one parquet per session with the full regular session of every listed symbol."""
    client = make_client()
    out_dir = out_dir or DATA_DIR / "paths"
    out_dir.mkdir(parents=True, exist_ok=True)
    for day, syms in symbols_by_day.items():       # caller controls the order
        path = out_dir / f"{day.isoformat()}.parquet"
        if path.exists():
            continue
        df = fetch_window(client, sorted(set(syms)), day, (9, 30), (16, 0), chunk=100)
        df.to_parquet(path)
        print(f"path {day}: {len(set(syms))} requested, {len(df)} rows", flush=True)


ALT_ETFS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "SHY", "LQD", "HYG", "GLD", "SLV", "DBC", "USO", "UUP", "VNQ",
            "XLE", "XLF", "XLK", "XLV", "XLP", "XLU", "XLY", "XLI", "XLB", "BIL", "AGG", "TIP", "VWO", "EWJ"]


def fetch_alt() -> None:
    """Dividend- and split-adjusted daily bars for a liquid multi-asset ETF set, plus SPY 1-minute history."""
    client = make_client()
    df = bars(client, ALT_ETFS, "1Day", HISTORY_START, HISTORY_END + "T23:59:59Z", adjustment="all")
    df.to_parquet(DATA_DIR / "alt_etf_daily.parquet")
    print(f"alt etf daily: {len(df)} rows, {df.symbol.nunique()} symbols", flush=True)
    out = DATA_DIR / "spy_1m"
    out.mkdir(parents=True, exist_ok=True)
    for year in range(2016, 2027):
        path = out / f"{year}.parquet"
        if path.exists():
            continue
        d = bars(client, ["SPY"], "1Min", f"{year}-01-01T00:00:00Z", f"{year}-12-31T23:59:59Z" if year < 2026 else HISTORY_END + "T23:59:59Z")
        d.to_parquet(path)
        print(f"spy 1m {year}: {len(d)} rows", flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "daily":
        fetch_daily(reverse="--reverse" in sys.argv)
    else:
        {"splits": fetch_splits, "alt": fetch_alt}[sys.argv[1]]()
