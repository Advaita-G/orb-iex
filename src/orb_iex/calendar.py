"""Market calendar and the daily session schedule.

Sessions come from Alpaca /v2/calendar, so holidays and early closes are
whatever Alpaca says. Times are built in America/New_York with zoneinfo, so
daylight-saving changes are handled by the timezone database.

Schedule (Main.cs, all relative to that day's open/close):
  open + 1 min            open sweep            (AfterMarketOpen(spy, 1))
  open + 5 min + delay    signal scan 09:35:03  (OnData at 09:35 + IEX delay)
  scan + diag delay       diagnostic re-request (~09:37, never trades)
  close - 60 s            cancel, wait, flatten (paper: exit at the 4:00 pm close)
  close - 20 s            close sweep
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from . import constants as C

NY = ZoneInfo(C.MARKET_TZ)


@dataclass(frozen=True)
class Session:
    day: date
    open: datetime   # aware, America/New_York
    close: datetime  # aware, America/New_York

    @property
    def is_early_close(self) -> bool:
        return self.close.time() < time(16, 0)


def _parse_hhmm(raw: str) -> time:
    h, m = raw.strip().split(":")[:2]
    return time(int(h), int(m))


def session_from_alpaca(row: dict) -> Session:
    d = date.fromisoformat(row["date"])
    o = datetime.combine(d, _parse_hhmm(row["open"]), tzinfo=NY)
    c = datetime.combine(d, _parse_hhmm(row["close"]), tzinfo=NY)
    if not o < c:
        raise ValueError(f"bad calendar row {row}")
    return Session(d, o, c)


class MarketCalendar:
    def __init__(self, sessions: Iterable[Session]):
        self._sessions = sorted(sessions, key=lambda s: s.day)
        self._by_day = {s.day: s for s in self._sessions}
        if not self._sessions:
            raise ValueError("empty market calendar")

    @classmethod
    def from_alpaca(cls, rows: list[dict]) -> "MarketCalendar":
        return cls(session_from_alpaca(r) for r in rows)

    @property
    def first_day(self) -> date:
        return self._sessions[0].day

    @property
    def last_day(self) -> date:
        return self._sessions[-1].day

    def session(self, day: date) -> Session | None:
        return self._by_day.get(day)

    def is_trading_day(self, day: date) -> bool:
        return day in self._by_day

    def next_session(self, day: date, include_today: bool = True) -> Session | None:
        for s in self._sessions:
            if s.day > day or (include_today and s.day == day):
                return s
        return None

    def previous_session(self, day: date) -> Session | None:
        prev = None
        for s in self._sessions:
            if s.day >= day:
                break
            prev = s
        return prev

    def sessions_before(self, day: date, count: int) -> list[Session]:
        """The `count` sessions strictly before `day`, oldest first."""
        before = [s for s in self._sessions if s.day < day]
        return before[-count:] if count > 0 else []

    def first_session_of_month(self, day: date) -> Session | None:
        for s in self._sessions:
            if s.day.year == day.year and s.day.month == day.month:
                return s
        return None

    def is_first_trading_day_of_month(self, day: date) -> bool:
        first = self.first_session_of_month(day)
        return first is not None and first.day == day

    def current_or_next(self, now: datetime) -> Session | None:
        """Today's session if it has not closed yet, otherwise the next one."""
        local = now.astimezone(NY)
        today = self.session(local.date())
        if today and local < today.close:
            return today
        return self.next_session(local.date(), include_today=False)

    def today(self, now: datetime) -> Session | None:
        return self.session(now.astimezone(NY).date())


@dataclass(frozen=True)
class Schedule:
    session: Session
    open_sweep: datetime
    scan: datetime
    scan_deadline: datetime
    diagnostic: datetime
    eod_exit: datetime
    close_sweep: datetime
    entry_cutoff: datetime

    @classmethod
    def for_session(cls, session: Session, scan_delay_s: int, diag_delay_s: int,
                    scan_grace_s: int, entry_cutoff_time: str = C.ENTRY_CUTOFF_DEFAULT) -> "Schedule":
        range_end = session.open + timedelta(minutes=C.OPENING_RANGE_MINUTES)
        scan = range_end + timedelta(seconds=scan_delay_s)
        hh, mm = int(entry_cutoff_time[:2]), int(entry_cutoff_time[3:])
        cutoff = session.open.astimezone(NY).replace(hour=hh, minute=mm, second=0, microsecond=0)
        cutoff = min(cutoff.astimezone(timezone.utc),
                     session.close - timedelta(minutes=C.ENTRY_CUTOFF_MIN_BEFORE_CLOSE_MIN))
        cutoff = max(cutoff, scan + timedelta(seconds=scan_grace_s) + timedelta(minutes=1))
        return cls(
            session=session,
            open_sweep=session.open + timedelta(minutes=C.OPEN_SWEEP_AFTER_OPEN_MIN),
            scan=scan,
            scan_deadline=scan + timedelta(seconds=scan_grace_s),
            diagnostic=range_end + timedelta(seconds=diag_delay_s),
            eod_exit=session.close - timedelta(seconds=C.EXIT_BEFORE_CLOSE_SECONDS),
            close_sweep=session.close - timedelta(seconds=C.CLOSE_SWEEP_BEFORE_CLOSE_SECONDS),
            entry_cutoff=cutoff,
        )

    def session_closing(self, now: datetime, scanned: bool) -> bool:
        """Main.cs _sessionClosing: false only between the scan and the EOD exit."""
        return not (scanned and self.scan <= now < self.eod_exit)

    def phase(self, now: datetime) -> str:
        if now < self.session.open:
            return "pre_open"
        if now < self.scan:
            return "opening_range"
        if now < self.eod_exit:
            return "active"
        if now < self.session.close:
            return "closing"
        return "after_close"
