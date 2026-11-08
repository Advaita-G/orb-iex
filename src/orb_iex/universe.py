"""Daily eligible universe (paper: all stocks listed on NYSE and Nasdaq).

Every trading day, before the open:
1. take Alpaca's active, tradable US-equity assets listed on NYSE, NYSE Arca,
   NYSE American or Nasdaq (common stocks and ETFs; OTC and odd instruments
   such as warrants/units with non-standard symbols are left out);
2. keep the names whose previous-14-day average volume is >= 1,000,000 and
   whose ATR(14) is > $0.50 (the paper's filters that use past data only).
Only these names need an opening bar at 09:35, where the opening price > $5,
relative volume >= 1 and top-20 rules are applied.
The day's eligible list is saved in the journal.
"""

from __future__ import annotations

import re
from datetime import date

from . import constants as C
from .indicators import atr_sma, average_daily_volume
from .marketdata import Bar

SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


def eligible_assets(assets: list[dict]) -> list[str]:
    return sorted(a["symbol"] for a in assets
                  if a.get("tradable") and a.get("status", "active") == "active"
                  and a.get("exchange") in C.UNIVERSE_EXCHANGES
                  and SYMBOL_RE.match(a.get("symbol") or ""))


def prefilter(daily: dict[str, dict[date, Bar]], today: date) -> list[dict]:
    """Names passing ADV(14) >= 1M and ATR(14) > $0.50 on sessions before today."""
    rows = []
    for sym, by_day in daily.items():
        bars = [by_day[d] for d in sorted(by_day) if d < today]
        atr = atr_sma(bars, C.LOOKBACK_DAYS)
        adv = average_daily_volume(bars, C.LOOKBACK_DAYS)
        if atr is not None and adv is not None and atr > C.MIN_ATR and adv >= C.MIN_AVERAGE_VOLUME:
            rows.append({"symbol": sym, "atr": atr, "adv": adv})
    rows.sort(key=lambda r: r["symbol"])
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows
