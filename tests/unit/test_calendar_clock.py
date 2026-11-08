from datetime import date, datetime, timedelta, timezone

import pytest

from helpers import calendar, et
from orb_iex.calendar import MarketCalendar, Schedule, session_from_alpaca
from orb_iex.clock import FakeClock


def sched(s):
    return Schedule.for_session(s, 3, 120, 60)


def test_normal_day_schedule():
    s = session_from_alpaca({"date": "2026-09-24", "open": "09:30", "close": "16:00"})
    sc = sched(s)
    assert sc.open_sweep == et(s.day, 9, 31)
    assert sc.scan == et(s.day, 9, 35, 3)
    assert sc.diagnostic == et(s.day, 9, 37)
    assert sc.eod_exit == et(s.day, 15, 59, 0)          # paper exits at the 4:00 pm close
    assert sc.close_sweep == et(s.day, 15, 59, 40)
    assert not s.is_early_close


def test_early_close_shifts_exit_times():
    s = session_from_alpaca({"date": "2026-11-27", "open": "09:30", "close": "13:00"})
    sc = sched(s)
    assert s.is_early_close
    assert sc.eod_exit == et(s.day, 12, 59) and sc.close_sweep == et(s.day, 12, 59, 40)


def test_dst_changes_utc_times():
    before = sched(session_from_alpaca({"date": "2026-03-06", "open": "09:30", "close": "16:00"}))
    after = sched(session_from_alpaca({"date": "2026-03-09", "open": "09:30", "close": "16:00"}))
    assert before.scan.astimezone(timezone.utc).hour == 14      # EST, UTC-5
    assert after.scan.astimezone(timezone.utc).hour == 13       # EDT, UTC-4
    autumn = sched(session_from_alpaca({"date": "2026-11-02", "open": "09:30", "close": "16:00"}))
    assert autumn.scan.astimezone(timezone.utc).hour == 14
    assert autumn.scan.astimezone(timezone.utc).minute == 35


def test_holiday_is_skipped():
    cal = calendar()
    assert not cal.is_trading_day(date(2026, 9, 7))              # Labor Day in the fake calendar
    assert cal.next_session(date(2026, 9, 7)).day == date(2026, 9, 8)
    assert cal.previous_session(date(2026, 9, 8)).day == date(2026, 9, 4)
    # overnight before the holiday: next session is after it
    assert cal.current_or_next(et(date(2026, 9, 4), 17, 0)).day == date(2026, 9, 8)


def test_first_trading_day_of_month_and_history_windows():
    cal = calendar()
    assert cal.is_first_trading_day_of_month(date(2026, 9, 1))
    assert not cal.is_first_trading_day_of_month(date(2026, 9, 2))
    assert cal.first_session_of_month(date(2026, 8, 15)).day == date(2026, 8, 3)   # Aug 1-2 weekend
    prev = cal.sessions_before(date(2026, 9, 8), 3)
    assert [p.day for p in prev] == [date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)]


def test_session_closing_window():
    s = session_from_alpaca({"date": "2026-09-24", "open": "09:30", "close": "16:00"})
    sc = sched(s)
    assert sc.session_closing(et(s.day, 9, 34), scanned=True)
    assert not sc.session_closing(et(s.day, 12, 0), scanned=True)
    assert sc.session_closing(et(s.day, 12, 0), scanned=False)
    assert not sc.session_closing(et(s.day, 15, 58), scanned=True)
    assert sc.session_closing(et(s.day, 15, 59), scanned=True)
    assert [sc.phase(et(s.day, h, m, x)) for h, m, x in ((9, 0, 0), (9, 33, 0), (12, 0, 0), (15, 59, 30),
                                                          (16, 1, 0))] == \
        ["pre_open", "opening_range", "active", "closing", "after_close"]


def test_fake_clock_moves_only_on_sleep():
    c = FakeClock(datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc))
    t0 = c.now()
    assert c.now() == t0
    c.sleep(90)
    assert c.now() - t0 == timedelta(seconds=90)
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 9, 24))


def test_bad_calendar_row_rejected():
    with pytest.raises(ValueError):
        session_from_alpaca({"date": "2026-09-24", "open": "16:00", "close": "09:30"})
    with pytest.raises(ValueError):
        MarketCalendar([])
