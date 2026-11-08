"""Deterministic metrics: the arithmetic, the sample-size gates and the missing-data rules.

These tests build trades directly (no journal on disk) so the expected values are
computed by hand and stated in the assertion, which is the point of the module: every
number is a pure function of the record, and a number that cannot be supported is
``None`` with a reason rather than a plausible-looking figure.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from orb_iex.dashboard import metrics
from orb_iex.dashboard import trades as trades_mod


def make_trade(day: str, symbol: str, direction: int, entry: str, exit_: str, qty: int,
               cost_per_share: str = "0.01", risk_per_share: str | None = None,
               entry_time: str | None = None, relvol: str | None = None, atr: str | None = None,
               opening: dict | None = None) -> trades_mod.Trade:
    """One flat-to-flat round trip, with the costs applied per share on both sides."""
    commission = slippage = D(cost_per_share) / 2
    entry_side, exit_side = ("buy", "sell") if direction > 0 else ("sell", "buy")
    fills = [
        {"order_id": f"{day}-{symbol}-entry", "symbol": symbol, "role": "entry", "side": entry_side,
         "qty": D(qty), "price": D(entry), "ts": entry_time or f"{day}T13:35:00Z", "cum_qty": str(qty),
         "commission_per_share": commission, "slippage_per_share": slippage},
        {"order_id": f"{day}-{symbol}-exit", "symbol": symbol, "role": "stop", "side": exit_side,
         "qty": D(qty), "price": D(exit_), "ts": f"{day}T15:00:00Z", "cum_qty": str(qty),
         "commission_per_share": commission, "slippage_per_share": slippage},
    ]
    risk = {(day, symbol): D(risk_per_share)} if risk_per_share else None
    closed, opened = trades_mod.group_fills(fills, default_commission=commission,
                                           default_slippage=slippage, risk_per_share=risk)
    assert not opened
    trade = trades_mod.Trade(raw=closed[0], symbol=symbol, day=day)
    trade.relvol = None if relvol is None else D(relvol)
    trade.atr = None if atr is None else D(atr)
    trade.opening = opening
    return trade


def series(pairs, cost_per_share="0.01") -> list[trades_mod.Trade]:
    """pairs: [(day, gross_dollars)] as one long round trip per session."""
    out = []
    for day, gross in pairs:
        qty = 100
        direction = 1 if gross >= 0 else 1
        entry = D("50.00")
        exit_ = entry + D(gross) / qty
        out.append(make_trade(day, "AAA", direction, str(entry), str(exit_), qty,
                              cost_per_share=cost_per_share))
    return out


# ---------------------------------------------------------------------------
# performance block
# ---------------------------------------------------------------------------

def test_perf_reports_the_sample_before_the_statistic():
    small = series([("2026-10-05", 100), ("2026-10-06", -50), ("2026-10-07", 20), ("2026-10-08", 5)])
    perf = metrics.perf(small, label="four trades")
    assert perf["trades"] == 4 and perf["trading_days"] == 4 and perf["days"] == [
        "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"]
    assert perf["valid"] is False and perf["sample_note"] == "fewer than 5 trades in this bucket"
    assert perf["expectancy_per_trade"] is None and perf["expectancy_r"] is None
    # The counts and the plainly-summed money are still reported: only the statistics
    # that need a sample are withheld.
    assert perf["wins"] == 3 and perf["losses"] == 1
    assert D(perf["realized_pnl"]) == D(75) and D(perf["modeled_cost"]) == D(8)
    assert D(perf["modeled_net_pnl"]) == D(67)
    assert perf["cost_per_trade"] == "2.00"


def test_expectancy_profit_factor_and_win_rate_are_exact():
    trades = series([("2026-10-01", 100), ("2026-10-02", 50), ("2026-10-05", -30),
                     ("2026-10-06", -30), ("2026-10-07", 20), ("2026-10-08", -10)])
    perf = metrics.perf(trades)
    assert perf["trades"] == 6 and perf["wins"] == 3 and perf["losses"] == 3
    assert D(perf["win_rate"]) == D("0.5")
    assert D(perf["realized_pnl"]) == D(100)              # the raw cost of the fills
    assert D(perf["modeled_cost"]) == D(12)               # 6 trades x (0.01 x 200 shares)
    assert D(perf["modeled_net_pnl"]) == D(88)
    # The statistics are computed on modeled net, never on the raw realized figure:
    # wins 98+48+18 = 164, losses 32+32+12 = 76.
    assert D(perf["profit_factor"]) == (D(164) / D(76)).quantize(D("0.01"))
    assert D(perf["expectancy_per_trade"]) == (D(88) / 6).quantize(D("0.01"))
    assert D(perf["average_win"]) == (D(164) / 3).quantize(D("0.01"))
    assert D(perf["average_loss"]) == (D(-76) / 3).quantize(D("0.01"))
    assert D(perf["best_trade"]) == D(98) and D(perf["worst_trade"]) == D(-32)
    assert perf["valid"] is True and perf["sample_note"] is None


def test_profit_factor_is_undefined_without_losses_not_infinite():
    perf = metrics.perf(series([("2026-10-01", 10)] * 5))
    assert perf["profit_factor"] is None
    assert perf["profit_factor_note"] == "no losing trades: profit factor is undefined, not infinite"


def test_recorded_fees_are_never_zero():
    perf = metrics.perf(series([("2026-10-01", 10)]))
    assert perf["recorded_fees"] is None
    assert perf["recorded_fees_note"] == "Alpaca paper charges no commission; only modeled costs exist"


def test_r_is_missing_where_the_risk_is_missing():
    trades = series([("2026-10-01", 50), ("2026-10-02", 50)])
    trades[0].raw["initial_risk"] = D(25)
    trades[0].raw["r"] = D(2)
    perf = metrics.perf(trades)
    assert perf["r_trades"] == 1 and perf["r_unavailable"] == 1
    assert D(perf["mean_r"]) == D(2) and D(perf["median_r"]) == D(2)


def test_cost_per_trade_notes_an_empty_bucket():
    perf = metrics.perf([])
    assert perf["trades"] == 0 and perf["modeled_cost"] == "0.00"
    assert perf["cost_per_trade"] is None and perf["cost_per_trade_note"] == "no trades to average over"
    assert perf["win_rate"] is None and perf["expectancy_per_trade"] is None


# ---------------------------------------------------------------------------
# ratios
# ---------------------------------------------------------------------------

def test_ratios_are_withheld_below_the_day_threshold():
    ratios = metrics.ratios(series([("2026-10-01", 100), ("2026-10-02", -50), ("2026-10-05", 20)]),
                            D(100_000))
    assert ratios["valid"] is False and ratios["days"] == 3
    assert ratios["sharpe"] is None and ratios["sortino"] is None
    assert "at least 20 are required" in ratios["reason"]


def test_ratios_need_a_base_equity_and_dispersion():
    twenty_five = series([(f"2026-09-{d:02d}", 100 if d % 2 else -60) for d in range(1, 26)])
    no_equity = metrics.ratios(twenty_five, None)
    assert no_equity["valid"] is False
    assert no_equity["reason"] == "no recorded base equity, so daily returns cannot be formed"

    flat = metrics.ratios(series([(f"2026-09-{d:02d}", 0) for d in range(1, 26)]), D(100_000))
    assert flat["valid"] is False and flat["reason"] == "zero dispersion in the daily returns"


def test_ratios_are_computed_when_the_sample_supports_them():
    twenty_five = series([(f"2026-09-{d:02d}", 200 if d % 2 else -60) for d in range(1, 26)])
    ratios = metrics.ratios(twenty_five, D(100_000))
    assert ratios["valid"] is True and ratios["days"] == 25
    assert D(ratios["sharpe"]) > 0 and D(ratios["sortino"]) > 0
    assert ratios["annualised_vol"] is not None
    assert D(ratios["sharpe"]).as_tuple().exponent == -2       # two decimals, no false precision


def test_sortino_is_withheld_when_downside_deviation_is_not_estimable():
    # One losing day in twenty-five: a downside deviation cannot be estimated from it.
    pairs = [(f"2026-09-{d:02d}", 200) for d in range(1, 26)]
    pairs[3] = ("2026-09-04", -60)
    ratios = metrics.ratios(series(pairs), D(100_000))
    assert ratios["sharpe"] is not None and ratios["sortino"] is None
    assert ratios["valid"] is False
    assert ratios["reason"] == "fewer than two losing days, so downside deviation is not estimable"


# ---------------------------------------------------------------------------
# equity curve and drawdown
# ---------------------------------------------------------------------------

def test_equity_curve_and_drawdown_follow_the_recorded_pnl():
    trades = series([("2026-10-01", 100), ("2026-10-02", -250), ("2026-10-05", 80)])
    curve = metrics.equity_curve(trades, D(100_000))
    assert [p["day"] for p in curve["points"]] == ["2026-10-01", "2026-10-02", "2026-10-05"]
    assert [p["cum_net"] for p in curve["points"]] == ["98.00", "-154.00", "-76.00"]
    assert curve["cum_realized"] == "-70.00" and curve["cum_net"] == "-76.00"
    assert curve["max_drawdown"] == "252.00"                   # 98 -> -154 from the peak
    assert curve["recorded_equity_note"] is not None            # no broker equity series is invented
    assert curve["base_equity"] == "100000"


def test_drawdown_preserves_intraday_losses_in_exit_order():
    trades = series([("2026-10-01", 100), ("2026-10-01", -250), ("2026-10-01", 200)],
                    cost_per_share="0")
    for t, stamp in zip(trades, ('15:00', '16:00', '17:00')):
        t.raw['exit_time'] = f'2026-10-01T{stamp}:00Z'
    # Daily aggregation sees +50 and would erase the $250 drawdown.
    curve = metrics.equity_curve(list(reversed(trades)), D(100_000))
    assert curve['cum_net'] == '50.00'
    assert curve['max_drawdown'] == '250.00'


def test_daily_pnl_keeps_the_views_apart():
    trades = series([("2026-10-01", 100), ("2026-10-01", 50), ("2026-10-02", -30)])
    assert metrics.daily_pnl(trades) == [("2026-10-01", D("146.00")), ("2026-10-02", D("-32.00"))]
    assert metrics.daily_pnl(trades, "realized") == [("2026-10-01", D("150.00")),
                                                    ("2026-10-02", D("-30.00"))]


def test_max_drawdown_measures_peak_to_trough():
    # cum: 10, -20, -15, -55. Peak 10 -> trough -55.
    assert metrics.max_drawdown([D(10), D(-30), D(5), D(-40)]) == D(65)
    assert metrics.max_drawdown([]) == D(0)
    assert metrics.max_drawdown([D(10), D(20)]) == D(0)


# ---------------------------------------------------------------------------
# buckets: a missing value is None, never a bucket of its own
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None, None), (D("0.9"), None), (D("1"), "1-2"), (D("1.99"), "1-2"), (D("2"), "2-3"),
    (D("9.99"), "5-10"), (D("10"), "10+"), (D("50"), "10+"),
])
def test_relvol_bucket_boundaries(value, expected):
    assert metrics.relvol_bucket(value) == expected


def test_the_other_buckets_and_their_missing_values():
    assert metrics.atr_pct_bucket(None) is None and metrics.atr_pct_bucket(D("0")) == "0-0.5%"
    assert metrics.delay_bucket(None) is None and metrics.delay_bucket(4.9) == "0-5s"
    assert metrics.delay_bucket(5) == "5-30s" and metrics.delay_bucket(600) == "120s+"
    assert metrics.spread_bucket(None) is None and metrics.spread_bucket(D("0.4")) == "0.25-0.5%"
    assert metrics.slippage_r_bucket(D("-0.01")) == "-inf..0R" and metrics.slippage_r_bucket(D("0.2")) == "0.1..0.25R"
    assert metrics.weekday_bucket("2026-10-05") == "Mon" and metrics.weekday_bucket(None) is None
    assert metrics.weekday_bucket("not-a-day") is None
    # 13:35 UTC in October is 09:35 in New York, which is where the session clock lives.
    assert metrics.hour_bucket("2026-10-05T13:35:00Z") == "09:35-10:00"
    assert metrics.hour_bucket("2026-10-05T18:30:00Z") == "14:00-15:00"
    assert metrics.hour_bucket(None) is None
    assert metrics.hour_bucket("nonsense") is None


def test_entry_delay_is_measured_from_the_scheduled_scan():
    # 09:35:03 ET by default; 13:35:09Z is 09:35:09 ET, so 6 seconds late.
    assert metrics.entry_delay_seconds("2026-10-05", "2026-10-05T13:35:09Z") == pytest.approx(6.0)
    assert metrics.entry_delay_seconds("2026-10-05", None) is None
    assert metrics.entry_delay_seconds("not-a-day", "2026-10-05T13:35:09Z") is None


# ---------------------------------------------------------------------------
# breakdowns, stress, incidents
# ---------------------------------------------------------------------------

def test_breakdowns_show_a_sample_size_per_bucket():
    trades = [
        make_trade("2026-10-05", "AAPL", 1, "50", "51", 100, relvol="2.5", atr="1.00",
                   opening={"open": "50.00"}),
        make_trade("2026-10-05", "MSFT", -1, "200", "201", 50, cost_per_share="0.02",
                   relvol="1.2", atr="2.00", opening={"open": "200.00"}),
        make_trade("2026-10-06", "AAPL", 1, "50", "51.5", 100, relvol="2.5", atr="1.00",
                   opening={"open": "50.00"}),
    ]
    for trade in trades:                              # a frozen R and a recorded exit reason
        trade.raw["initial_risk"] = D(50)
        trade.raw["r"] = trade.raw["gross_pnl"] / D(50)
        trade.exit_reason = "stop"
    trades[2].exit_reason = "eod"
    blocks = metrics.breakdowns(trades, spreads={"2026-10-05:AAPL": D("0.05")},
                               delays={"2026-10-05:AAPL": 2.0})
    by_symbol = blocks["by_symbol"]
    assert set(by_symbol) == {"AAPL", "MSFT"}
    assert by_symbol["AAPL"]["trades"] == 2 and by_symbol["AAPL"]["valid"] is False
    assert by_symbol["AAPL"]["sample_note"]  # two trades is not a bucket you can conclude from
    assert D(by_symbol["AAPL"]["mean_r"]) == D("2.50")   # +2.00 and +3.00 on a 50/share risk
    assert set(blocks["by_side"]) == {"long", "short"}
    assert blocks["by_relvol"]["2-3"]["trades"] == 2 and blocks["by_relvol"]["1-2"]["trades"] == 1
    # ATR as a share of price: 1.00/50.00 = 2% (two trades) and 2.00/200.00 = 1% (one).
    assert blocks["by_atr_pct"]["2-5%"]["trades"] == 2 and blocks["by_atr_pct"]["1-2%"]["trades"] == 1
    assert blocks["by_weekday"]["Mon"]["trades"] == 2
    assert blocks["by_time_of_day"]["09:35-10:00"]["trades"] == 3
    assert blocks["by_exit_reason"]["stop"]["trades"] == 2
    assert blocks["by_exit_reason"]["eod"]["trades"] == 1
    assert blocks["by_entry_delay"]["0-5s"]["trades"] == 1
    assert blocks["by_spread"]["0-0.1%"]["trades"] == 1
    assert blocks["by_slippage"] == {}                            # no slippage measured: no bucket,
    #                                                              not a "0" bucket
    assert list(blocks["by_regime"]) == ["_unavailable"]
    assert "no market/volatility regime is journaled" in blocks["by_regime"]["_unavailable"]["reason"]


def test_stress_shows_what_a_wrong_cost_model_would_cost():
    stress = metrics.stress(series([("2026-10-01", 100), ("2026-10-02", 100)]))
    assert stress["realized_pnl"] == "200.00" and stress["modeled_cost_1x"] == "4.00"
    assert stress["modeled_net_2x"] == "192.00" and stress["modeled_net_3x"] == "188.00"


def test_incidents_by_day_counts_flags_by_kind():
    trades = series([("2026-10-01", 10), ("2026-10-01", -10), ("2026-10-02", 5)])
    trades[0].execution_defects = ["stop_rejected", "entry_unprotected"]
    trades[0].broker_flags = ["order_rejected"]
    trades[1].loss_class = "mixed"
    counted = metrics.incidents_by_day(trades)
    # Counted per affected trade, not per flag: one trade with two defects is one defect trade.
    assert counted["2026-10-01"] == {"trades": 2, "defects": 1, "broker": 1, "data": 0, "mixed": 1}
    assert counted["2026-10-02"]["defects"] == 0


def test_dist_and_cvar_describe_the_tail():
    values = [float(x) for x in (10, 20, 30, 40, -50)]
    described = metrics.dist(values)
    assert described["n"] == 5 and described["min"] == -50.0 and described["max"] == 40.0
    assert metrics.dist([]) == {"n": 0, "min": None, "p50": None, "p95": None, "max": None}
    # Fewer than five observations is not a tail, it is an anecdote.
    assert metrics.cvar([D(10), D(-10), D(-20), D(30)]) is None
    assert metrics.cvar([D(10), D(-10), D(-20), D(30), D(5)]) == D(-20)
    # 40 observations: the bottom 5% is two of them, averaged.
    assert metrics.cvar([D(v) for v in range(-20, 20)]) == D("-19.5")
