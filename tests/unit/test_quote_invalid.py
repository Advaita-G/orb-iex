"""Malformed upstream prices/timestamps must never reach entry arithmetic."""
import pytest

from orb_iex.marketdata import parse_quote


@pytest.mark.parametrize("value", ["Infinity", "-Infinity", "NaN", "sNaN"])
def test_nonfinite_quote_side_is_unavailable(value):
    q = parse_quote("AAA", {"bp": value, "ap": "50", "t": "2026-10-28T13:35:00Z"})
    assert q is None or q.bid is None


def test_quote_without_timezone_is_unavailable():
    assert parse_quote("AAA", {"bp": "49", "ap": "50", "t": "2026-10-28T13:35:00"}) is None
