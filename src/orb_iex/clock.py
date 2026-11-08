"""Injectable clocks. All strategy code asks a Clock for the time.

SystemClock uses the real time. FakeClock is for tests and replays: time
moves only when the code sleeps or the test advances it.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...          # timezone-aware, UTC
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock:
    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware start time")
        self._now = start.astimezone(timezone.utc)
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=max(0.0, seconds))

    def set(self, when: datetime) -> None:
        self._now = when.astimezone(timezone.utc)
