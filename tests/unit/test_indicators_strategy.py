from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from helpers import bar, et
from orb_iex import constants as C
from orb_iex.indicators import atr_sma, average_daily_volume, relative_volume, sma, true_range
from orb_iex.marketdata import parse_bar
from orb_iex.strategy import (Features, compute_features, lean_calculate_order_quantity, plan_entry,
                              reject_reason, round_levels, select_candidates, size_position)

DAY0 = date(2026, 9, 1)


def daily(n, h=51, l=49, c=50, v=2_000_000):
    return [bar(et(DAY0 + timedelta(days=i), 9, 30), c, h, l, c, v) for i in range(n)]


# --- ATR / ADV / RelVol -------------------------------------------------------

def test_true_range_uses_previous_close():
    b = bar(et(DAY0, 9, 30), 10, 12, 9, 11, 1)
    assert true_range(b, None) == D(3)
    assert true_range(b, D(15)) == D(6)     # gap down: |low - prev close|
    assert true_range(b, D(5)) == D(7)      # gap up: |high - prev close|


def test_atr_is_simple_mean_not_wilder():
    bars = daily(15)
    assert atr_sma(bars) == D(2)
    # one wide day at the end moves a simple average by exactly (10-2)/14
    bars[-1] = bar(bars[-1].t, 50, 55, 45, 50, 1)
    assert atr_sma(bars) == D(2) + D(8) / D(14)
    assert atr_sma(daily(14)) is None       # needs 15 bars for 14 true ranges


def test_atr_uses_only_last_14_true_ranges():
    bars = [bar(et(DAY0, 9, 30), 50, 80, 20, 50, 1)] + daily(15)
    assert atr_sma(bars) == D(2)


def test_average_daily_volume():
    assert average_daily_volume(daily(13)) is None
    vols = daily(20)
    vols[-1] = bar(vols[-1].t, 50, 51, 49, 50, 3_400_000)
    assert average_daily_volume(vols) == (D(13 * 2_000_000) + D(3_400_000)) / 14


def test_relative_volume_excludes_today():
    prev = [1000] * 14
    assert relative_volume(2000, prev) == D(2)
    assert relative_volume(2000, [1000] * 13) is None
    assert relative_volume(2000, [0] * 14) is None
    # only the last 14 previous values count
    assert relative_volume(3000, [99999] + [1500] * 14) == D(2)
    assert sma([1, 2, 3], 2) == D("2.5")


def test_compute_features_skips_missing_days_no_warmup_wait():
    today = DAY0 + timedelta(days=40)
    openings = {DAY0 + timedelta(days=i): bar(et(DAY0 + timedelta(days=i), 9, 30), 50, 51, 49, 50, 1000)
                for i in range(30) if i % 3}                 # every 3rd day missing
    dly = {DAY0 + timedelta(days=i): b for i, b in enumerate(daily(30))}
    t = bar(et(today, 9, 30), 50, 51, 49, 50.5, 2500)
    f = compute_features("X", t, dly, openings, today)
    assert f.relvol == D("2.5") and f.atr == D(2) and f.adv == D(2_000_000) and f.ready
    assert f.opening_history == 20 and f.daily_history == 30


def test_bad_bars_are_rejected():
    good = {"t": "2026-09-24T13:30:00Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 100}
    assert parse_bar(good) is not None
    for bad in ({**good, "h": 8}, {**good, "l": 12}, {**good, "o": 0}, {**good, "v": -1},
                {**good, "c": "nan"}, {**good, "v": 1.5}, {k: v for k, v in good.items() if k != "c"},
                {**good, "t": "garbage"}, {**good, "v": True}):
        assert parse_bar(bad) is None, bad


# --- candidates -----------------------------------------------------------------

def feat(sym, relvol, o=50, c=50.5, atr=2, adv=2_000_000, h=51, l=49.5):
    return Features(sym, bar(et(DAY0, 9, 30), o, h, l, c, 1000), D(str(relvol)), D(str(atr)), D(str(adv)))


def test_filters_match_main_cs_thresholds():
    assert reject_reason(feat("A", 1)) is None                        # relvol >= 1 passes
    assert reject_reason(feat("A", "0.99")) == "relvol<1"
    assert reject_reason(feat("A", 2, atr="0.50")) == "atr<=0.50"     # strict >
    assert reject_reason(feat("A", 2, adv=999_999)) == "adv<1M"
    assert reject_reason(feat("A", 2, adv=1_000_000)) is None
    assert reject_reason(feat("A", 2, o=5, c=5.1, h=5.2, l=4.9)) == "open<=5"
    assert reject_reason(Features("A", None, None, D(1), D(1))).startswith("not_ready")


def test_ranking_relvol_desc_then_ticker_and_top20():
    fs = [feat(f"T{i:02d}", 1 + i % 7) for i in range(30)] + [feat("AAA", 7), feat("ZZZ", 7)]
    top, table = select_candidates(fs)
    assert len(top) == C.MAX_POSITIONS
    rels = [f.relvol for f in top]
    assert rels == sorted(rels, reverse=True)
    sevens = [f.symbol for f in top if f.relvol == 7]
    assert sevens == sorted(sevens) and sevens[0] == "AAA"
    below = [r for r in table if r["status"] == "below_top20"]
    assert len(below) == 32 - 20


# --- direction / entry / sizing -------------------------------------------------

def test_long_short_flat():
    lp = plan_entry(feat("L", 2, o=50, c=50.5, h=51, l=49.5), D(100000), D(1), D("0.0035"))
    assert lp.direction == 1 and lp.entry_price == D("51.00") and lp.stop_price == D("50.80")
    sp = plan_entry(feat("S", 2, o=50, c=49.5, h=50.5, l=49), D(100000), D(1), D("0.0035"))
    assert sp.direction == -1 and sp.entry_price == D("49.00") and sp.stop_price == D("49.20")
    assert plan_entry(feat("F", 2, o=50, c=50), D(100000), D(1), D("0.0035")) == "flat_bar"


def test_entry_placed_even_when_close_is_at_the_level():
    # paper: the stop order is placed at the range high/low; no "already through" skip
    lp = plan_entry(feat("L", 2, o=50, c=51, h=51, l=49.5), D(100000), D(4), D(0))
    sp = plan_entry(feat("S", 2, o=50, c=49, h=50.5, l=49), D(100000), D(4), D(0))
    assert lp.entry_price == D("51.00") and sp.entry_price == D("49.00")


def test_sub_penny_levels_rounded_away_from_market():
    e, s = round_levels(1, D("495.205"), D("495.0012"))
    assert e == D("495.21") and s == D("495.00")
    e, s = round_levels(-1, D("221.135"), D("221.3378"))
    assert e == D("221.13") and s == D("221.34")


def test_sizing_4x_gross_cap_shared_by_20_positions():
    # 4x / 20 = 20% of equity per position. Tight stop: risk qty 250, cap 20% -> 400 at $50
    q, info = size_position(D(100000), D(4), D(50), D("0.20"), D(50), D(0))
    assert info["weight"] == D("0.2") and info["notional_cap_qty"] == 400
    assert q == 250 and info["binding"] == "risk"
    # very tight stop: the 4x/20 cap binds, 20 full positions = 20 x 20% = 4x
    q, info = size_position(D(100000), D(4), D(50), D("0.05"), D(50), D(0))
    assert info["risk_qty"] == 1000 and q == 399 and q * 50 <= 100000 * 4 / 20


def test_sizing_minimum_of_three_limits():
    # risk binds: 100k/20 = 5000 alloc, 1% = 50 risk, stop distance 2 -> 25 shares
    q, info = size_position(D(100000), D(1), D(50), D(2), D(49.5), D("0.0035"))
    assert q == 25 and info["binding"] == "risk"
    # tight stop: notional cap binds: 100000*0.05/50 = 100
    q, info = size_position(D(100000), D(1), D(50), D("0.20"), D("49.9"), D(0))
    assert info["risk_qty"] == 250 and info["notional_cap_qty"] == 100 and q <= 100
    # LEAN CalculateOrderQuantity keeps the 0.25% free buffer and fees inside the target
    assert lean_calculate_order_quantity(D(100000), D("0.05"), D(50), D(0)) == 99
    q, info = size_position(D(100000), D(1), D(50), D("0.20"), D(50), D(0))
    assert q == 99 and info["binding"] == "weight_calc"


def test_sizing_uses_gross_exposure_weight():
    q, info = size_position(D(100000), D("0.5"), D(50), D("0.20"), D(50), D(0))
    assert info["weight"] == D("0.025") and q == 49


def test_zero_quantity_skipped():
    assert plan_entry(feat("L", 2, o=50, c=50.5, h=51, l=49.5, atr=2), D(100), D(1), D(0)) == "zero_quantity"
