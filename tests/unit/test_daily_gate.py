from datetime import date, datetime, timezone
from pathlib import Path

from orb_iex.daily_gate import (MAX_ATTEMPTS, close_time, is_early_close, is_trading_day, nyse_holidays,
                                pending_days)

HOLIDAYS_2026 = {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
                 date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25)}


def test_2026_holidays_match_the_published_nyse_calendar():
    assert set(nyse_holidays(2026)) == HOLIDAYS_2026


def test_2027_new_year_saturday_is_not_observed_on_friday():
    assert date(2026, 12, 31) not in nyse_holidays(2026)
    assert date(2027, 12, 24) in nyse_holidays(2027)          # Christmas Saturday -> Friday


def test_early_closes_close_at_one():
    assert is_early_close(date(2026, 11, 27)) and close_time(date(2026, 11, 27)).hour == 13
    assert is_early_close(date(2026, 12, 24)) and is_early_close(date(2025, 7, 3))
    assert not is_early_close(date(2026, 7, 2)) and not is_early_close(date(2026, 11, 26))
    assert close_time(date(2026, 10, 26)).hour == 16


def test_weekends_and_holidays_are_not_trading_days():
    assert not is_trading_day(date(2026, 10, 24)) and not is_trading_day(date(2026, 11, 26))
    assert is_trading_day(date(2026, 10, 26))


def utc(s):
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def test_review_is_due_only_after_close_plus_buffer(tmp_path: Path):
    assert date(2026, 10, 26) not in [p.day for p in pending_days(utc("2026-10-26T16:30:00-04:00"), tmp_path)]
    assert date(2026, 10, 26) in [p.day for p in pending_days(utc("2026-10-26T16:41:00-04:00"), tmp_path)]


def test_missed_days_are_caught_up_oldest_first_and_skip_weekend_and_holiday(tmp_path: Path):
    days = [p.day for p in pending_days(utc("2026-11-30T17:00:00-05:00"), tmp_path)]
    assert days == [date(2026, 11, 27), date(2026, 11, 30)]


def test_finished_and_exhausted_days_are_not_pending(tmp_path: Path):
    (tmp_path / "daily-review-2026-10-26.md").write_text("x")
    (tmp_path / "daily-review-2026-10-27.attempts").write_text(str(MAX_ATTEMPTS))
    days = [p.day for p in pending_days(utc("2026-10-28T17:00:00-04:00"), tmp_path)]
    assert days == [date(2026, 10, 28)]
