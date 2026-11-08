"""Pure indicator math (Decimal). Mirrors the LEAN indicators Main.cs uses.

- ATR(14): LEAN AverageTrueRange with MovingAverageType.Simple = simple mean
  of the last 14 true ranges (NOT Wilder smoothing).
- Average daily volume: SimpleMovingAverage(14) of daily volumes.
- Relative opening volume: today's opening-bar volume divided by the SMA of
  the PREVIOUS 14 opening-bar volumes. Today is never in the denominator
  (Main.cs computes the ratio before it updates the SMA). Days without an
  opening bar are skipped, not zero-filled.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from .marketdata import Bar


def true_range(bar: Bar, prev_close: Decimal | None) -> Decimal:
    if prev_close is None:
        return bar.h - bar.l
    return max(bar.h - bar.l, abs(bar.h - prev_close), abs(bar.l - prev_close))


def atr_sma(bars: Sequence[Bar], period: int = 14) -> Decimal | None:
    """ATR from daily bars, oldest first. Needs period + 1 bars so every one of
    the last `period` true ranges has a previous close (steady-state LEAN value)."""
    if len(bars) < period + 1:
        return None
    window = bars[-(period + 1):]
    trs = [true_range(window[i], window[i - 1].c) for i in range(1, len(window))]
    return sum(trs, Decimal(0)) / Decimal(period)


def sma(values: Sequence[Decimal | int], period: int) -> Decimal | None:
    if period <= 0 or len(values) < period:
        return None
    window = [Decimal(v) for v in values[-period:]]
    return sum(window, Decimal(0)) / Decimal(period)


def average_daily_volume(bars: Sequence[Bar], period: int = 14) -> Decimal | None:
    return sma([b.v for b in bars], period)


def relative_volume(today_volume: int, previous_volumes: Sequence[int], period: int = 14) -> Decimal | None:
    """previous_volumes: earlier opening-bar volumes, oldest first, today excluded."""
    avg = sma(previous_volumes, period)
    if avg is None or avg <= 0:
        return None
    return Decimal(today_volume) / avg
