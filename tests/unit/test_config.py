import pytest
from decimal import Decimal

from orb_iex import constants as C
from orb_iex.config import ConfigError, load_settings, parse_settings


def test_defaults_mirror_main_cs():
    s = parse_settings({})
    assert s.universe_size == 200
    assert s.max_gross_exposure == Decimal("4")          # paper: max leverage 4x
    assert s.commission_per_share == Decimal("0.0035")
    assert s.slippage_per_share == Decimal("0.01")
    assert s.variant == "ORB-IEX"
    assert (s.opening_feed, s.history_feed, s.adjustment) == ("iex", "sip", "raw")
    assert C.MAX_POSITIONS == 20 and not hasattr(C, "MAX_TRADE_WEIGHT")
    assert C.LOOKBACK_DAYS == 14 and C.OPENING_RANGE_MINUTES == 5


def test_shipped_settings_file_is_valid():
    from pathlib import Path
    s = load_settings(Path(__file__).resolve().parents[2] / "config/settings.toml")
    assert s.mode == "readonly"
    assert "tradingHost=paper-api.alpaca.markets" in s.config_line()


@pytest.mark.parametrize("raw, fragment", [
    ({"strategy": {"universe_sise": 100}}, "unknown key"),
    ({"stratgy": {}}, "unknown section"),
    ({"strategy": {"universe_size": "200"}}, "must be an integer"),
    ({"strategy": {"universe_size": True}}, "must be an integer"),
    ({"strategy": {"universe_size": 0}}, "must be >"),
    ({"strategy": {"max_gross_exposure": 0}}, "must be >"),
    ({"strategy": {"max_gross_exposure": 4.5}}, "must be <="),
    ({"strategy": {"max_gross_exposure": "abc"}}, "not a number"),
    ({"strategy": {"variant": "ORB-SIP"}}, "not allowed"),
    ({"data": {"opening_feed": "sip"}}, "not allowed"),
    ({"data": {"history_feed": "iex"}}, "not allowed"),
    ({"data": {"adjustment": "split"}}, "not allowed"),
    ({"data": {"history_min_age_minutes": 10}}, "must be >="),
    ({"data": {"market_data_calls_per_minute": 500}}, "must be <="),
    ({"runtime": {"mode": "live"}}, "must be one of"),
])
def test_bad_settings_fail_loudly(raw, fragment):
    with pytest.raises(ConfigError) as exc:
        parse_settings(raw)
    assert fragment in str(exc.value)


def test_all_errors_reported_together():
    with pytest.raises(ConfigError) as exc:
        parse_settings({"strategy": {"universe_size": -1, "max_gross_exposure": 9}})
    msg = str(exc.value)
    assert "universe_size" in msg and "max_gross_exposure" in msg


def test_no_trading_url_setting_exists():
    # The trading host must not be configurable at all.
    for section in ("runtime", "trading", "safety"):
        with pytest.raises(ConfigError):
            parse_settings({section: {"base_url": "https://api.alpaca.markets"}})
