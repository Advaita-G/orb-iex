import numpy as np
import pandas as pd

from quantlab.metrics import summarize
from quantlab.orb import (BASE_COSTS, PAPER_COSTS, Costs, OrbSpec, Sizing, add_rvol, opening_ranges, run_portfolio,
                          select_candidates, simulate_trade, size_day)

NY = "America/New_York"


def mins(rows, day="2024-03-05"):
    """rows: (minute_after_open, o,h,l,c,v)"""
    base = pd.Timestamp(f"{day} 09:30", tz=NY)
    return pd.DataFrame([{"symbol": "X", "t": (base + pd.Timedelta(minutes=m)).tz_convert("UTC"), "o": o, "h": h,
                          "l": l, "c": c, "v": v} for m, o, h, l, c, v in rows])


def path_after_or(rows):  # bars from 09:35 on
    return mins(rows)


SPEC = OrbSpec()
FREE = Costs(commission=0.0, slippage=0.0)


def test_long_breakout_stop_and_eod():
    atr = 2.0                                    # stop distance 0.2
    p = path_after_or([(5, 10.0, 10.3, 9.95, 10.2, 1), (6, 10.2, 10.5, 10.3, 10.4, 1), (7, 10.4, 10.9, 10.4, 10.8, 1)])
    t = simulate_trade(p, 1, 10.2, atr, 9.0, SPEC, FREE)
    # triggers at 09:35 (high 10.3 >= 10.2); entry bar low 9.95 <= stop 10.0: closed 10.2 > stop -> ambiguous
    assert t["reason"] == "stop_same_bar" and t["ambiguous_entry_bar"] and abs(t["r"] + 1) < 1e-9
    opt = simulate_trade(p, 1, 10.2, atr, 9.0, OrbSpec(same_bar="optimistic"), FREE)
    assert opt["reason"] == "eod" and abs(opt["exit"] - 10.8) < 1e-9 and opt["r"] > 2


def test_gap_through_entry_and_gap_through_stop_fill_at_open():
    atr = 2.0
    p = path_after_or([(5, 10.5, 10.6, 10.45, 10.55, 1), (6, 10.0, 10.1, 9.9, 9.95, 1), (7, 9.9, 9.95, 9.8, 9.85, 1)])
    t = simulate_trade(p, 1, 10.2, atr, 9.0, SPEC, FREE)
    assert abs(t["entry"] - 10.5) < 1e-9                     # opened through the trigger: fill at the open
    assert t["reason"] == "stop" and abs(t["exit"] - 10.0) < 1e-9 and t["r"] < -1.0   # stop 10.3, gapped to 10.0


def test_short_breakout_with_costs():
    atr = 2.0
    p = path_after_or([(5, 9.9, 9.95, 9.7, 9.8, 1), (6, 9.8, 9.82, 9.0, 9.1, 1)])
    t = simulate_trade(p, -1, 9.8, atr, 11.0, SPEC, BASE_COSTS)
    # entry at 9.8 - slip 0.01 = 9.79, EOD close 9.1 + 0.01 = 9.11, pnl 0.68 - 2*0.0035 = 0.673
    assert abs(t["entry"] - 9.79) < 1e-9 and abs(t["pnl_ps"] - (9.79 - 9.11 - 0.007)) < 1e-9


def test_cutoff_blocks_late_trigger_and_no_trigger_returns_none():
    p = path_after_or([(5, 10.0, 10.1, 9.9, 10.0, 1), (40, 10.0, 10.5, 10.0, 10.4, 1)])
    assert simulate_trade(p, 1, 10.3, 2.0, 9.0, OrbSpec(cutoff_minute=9 * 60 + 50), FREE) is None
    assert simulate_trade(p, 1, 10.3, 2.0, 9.0, OrbSpec(), FREE) is not None
    assert simulate_trade(p, 1, 11.0, 2.0, 9.0, OrbSpec(), FREE) is None


def test_opening_range_and_rvol_exclude_today_and_skip_missing_sessions():
    rows = []
    for i, d in enumerate(pd.bdate_range("2024-03-01", periods=16)):
        if i == 5:
            continue                                         # a missing session is skipped, not zero-filled
        base = pd.Timestamp(f"{d.date()} 09:30", tz=NY)
        for m in range(5):
            rows.append({"symbol": "X", "t": (base + pd.Timedelta(minutes=m)).tz_convert("UTC"), "o": 10, "h": 11,
                         "l": 9, "c": 10.5, "v": 100 if i < 15 else 1000})
    op = add_rvol(opening_ranges(pd.DataFrame(rows), 5), lookback=14, min_history=10)
    last = op.iloc[-1]
    assert last.or_vol == 5000 and abs(last.rvol - 5000 / 500) < 1e-9     # denominator excludes today
    assert op.rvol.iloc[:10].isna().all()                                  # < 10 prior ranges -> undefined


def table(**kw):
    base = dict(symbol=["A", "B", "C", "D"], or_open=[10, 10, 10, 4], or_close=[11, 9, 10, 4.5], atr14=[1, 1, 1, 1],
                adv14=[2e6] * 4, prev_close=[10] * 4, rvol=[3, 5, 9, 9], split_window=[False] * 4)
    base.update(kw)
    return pd.DataFrame(base)


def test_candidate_selection_applies_published_filters_and_rank():
    c = select_candidates(table(), OrbSpec(top_n=20))
    assert list(c.symbol) == ["B", "A"]                       # C is a doji, D is under $5; rank by RVOL desc
    assert list(c.direction) == [-1, 1]
    assert list(select_candidates(table(), OrbSpec(top_n=1)).symbol) == ["B"]
    assert list(select_candidates(table(), OrbSpec(min_rvol=4)).symbol) == ["B"]
    assert len(select_candidates(table(split_window=[True, False, False, False]), OrbSpec())) == 1


def test_sizing_modes_and_leverage_cap():
    tr = pd.DataFrame({"entry": [50.0] * 20, "stop_dist": [0.1] * 20})
    paper = size_day(tr, 100_000, Sizing(mode="paper"))
    assert (paper * tr.entry).sum() <= 4 * 100_000 + 1e-6 and paper.min() > 0
    prod = size_day(tr, 100_000, Sizing(mode="production"))
    assert (prod * tr.entry).max() <= 4 * 100_000 / 20 + 1e-6
    small = size_day(pd.DataFrame({"entry": [50.0], "stop_dist": [2.0]}), 100_000, Sizing(mode="paper"))
    assert small.iloc[0] == 500                                # 1% of 100k / $2 stop


def test_portfolio_compounds_and_summarize_counts_zero_days():
    trades = pd.DataFrame({"date": pd.to_datetime(["2024-03-04", "2024-03-05"]), "symbol": ["A", "A"],
                           "entry": [50.0, 50.0], "stop_dist": [1.0, 1.0], "pnl_ps": [2.0, -1.0], "r": [2.0, -1.0]})
    rows, curve = run_portfolio(trades, Sizing(mode="paper", start_equity=100_000))
    assert abs(curve.pnl.iloc[0] - 1000 * 2.0) < 1e-6 and curve.equity_before.iloc[1] == 102_000
    sessions = pd.bdate_range("2024-03-04", "2024-03-08")
    s = summarize(curve, rows, sessions, 100_000)
    assert s["trades"] == 2 and s["sessions"] == 5 and s["win_rate"] == 0.5


def test_rvol_baseline_rejected_when_prior_observations_are_far_apart():
    all_days = list(pd.bdate_range("2024-01-02", "2024-06-07"))          # Z defines the session calendar
    x_days = set(list(pd.bdate_range("2024-01-02", periods=12)) + list(pd.bdate_range("2024-06-03", periods=3)))
    rows = []
    for d in all_days:
        base = pd.Timestamp(f"{d.date()} 09:30", tz=NY).tz_convert("UTC")
        for sym in ("Z", "X"):
            if sym == "X" and d not in x_days:
                continue
            rows.append({"symbol": sym, "t": base, "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 100})
    op = add_rvol(opening_ranges(pd.DataFrame(rows), 5), lookback=14, min_history=10)
    x = op[op.symbol == "X"].set_index("date")
    assert pd.notna(x.loc["2024-01-17", "rvol"])                        # 10 prior observations inside 11 sessions
    assert pd.isna(x.loc["2024-06-03", "rvol"])                         # its 10th-prior observation is ~5 months back
    z = op[op.symbol == "Z"].set_index("date")
    assert pd.notna(z.loc["2024-06-03", "rvol"])
