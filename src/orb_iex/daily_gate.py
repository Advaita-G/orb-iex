"""Which after-close days still need the daily review. Offline, stdlib only, read-only.

The launchd job calls ``orb_iex daily-gate`` and reviews each printed day in order. A day is
pending when it was a NYSE session, its close plus a buffer has passed, no
``daily-review-<day>.md`` exists, and fewer than ``MAX_ATTEMPTS`` runs have failed. A missed
run (Mac asleep or off) is therefore picked up on the next launch, up to ``LOOKBACK_DAYS`` back.

Holiday rules are the published NYSE ones. They are only used to decide whether a review is
due: the bot itself takes its calendar from Alpaca (``calendar.py``) and never reads this file.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
LOOKBACK_DAYS = 3
MAX_ATTEMPTS = 3
CLOSE_BUFFER = timedelta(minutes=40)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month, day = divmod(h + el - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(day: date, *, saturday_to_friday: bool = True) -> date | None:
    if day.weekday() == 5:
        return day - timedelta(days=1) if saturday_to_friday else None
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def nyse_holidays(year: int) -> dict[date, str]:
    """Full-day NYSE closures in ``year`` (weekday dates only)."""
    fixed = [(date(year, 1, 1), "New Year's Day", False), (date(year, 7, 4), "Independence Day", True),
             (date(year, 12, 25), "Christmas", True)]
    if year >= 2022:
        fixed.append((date(year, 6, 19), "Juneteenth", True))
    out: dict[date, str] = {}
    for day, name, sat_to_fri in fixed:
        obs = _observed(day, saturday_to_friday=sat_to_fri)
        if obs and obs.year == year:
            out[obs] = name
    out[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    out[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    out[_easter(year) - timedelta(days=2)] = "Good Friday"
    out[_last_weekday(year, 5, 0)] = "Memorial Day"
    out[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    out[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving"
    return out


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in nyse_holidays(day.year)


def is_early_close(day: date) -> bool:
    """13:00 ET: the day after Thanksgiving, July 3 (Mon-Thu) and Dec 24 (weekday)."""
    if not is_trading_day(day):
        return False
    thanksgiving = _nth_weekday(day.year, 11, 3, 4)
    return (day == thanksgiving + timedelta(days=1)
            or (day.month == 7 and day.day == 3 and day.weekday() < 4)
            or (day.month == 12 and day.day == 24))


def close_time(day: date) -> datetime:
    return datetime.combine(day, time(13 if is_early_close(day) else 16), tzinfo=NY)


@dataclass(frozen=True)
class Pending:
    day: date
    reason: str


def attempts(reports_dir: Path, day: date) -> int:
    path = reports_dir / f"daily-review-{day}.attempts"
    try:
        return int(path.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def pending_days(now: datetime, reports_dir: Path) -> list[Pending]:
    """Oldest first. Each is a closed session without a report and with attempts left."""
    now_ny = now.astimezone(NY)
    out = []
    for back in range(LOOKBACK_DAYS, -1, -1):
        day = now_ny.date() - timedelta(days=back)
        if not is_trading_day(day) or now_ny < close_time(day) + CLOSE_BUFFER:
            continue
        if (reports_dir / f"daily-review-{day}.md").exists():
            continue
        if attempts(reports_dir, day) >= MAX_ATTEMPTS:
            continue
        out.append(Pending(day, "early close" if is_early_close(day) else "session"))
    return out
