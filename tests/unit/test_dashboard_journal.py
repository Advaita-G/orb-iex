"""The journal dashboard against a real journal: grouping, accounting, R, funnel, calendar.

Every test here reads the fixture in ``tests/dashboard_fixture.py``, which is built
with the engine's own SQLite schemas, so the assertions run against the same contract
the live bot writes.
"""

from __future__ import annotations

import json
from decimal import Decimal as D

import dashboard_fixture as fx
import pytest

from orb_iex.dashboard.api import Filters, Journal
from orb_iex.dashboard import trades as trades_mod
from orb_iex.reconcile import fills_from_journal, reconstruct

TRADES_SCHEMA = fx.TRADES


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    base = tmp_path_factory.mktemp("journal-root")
    fx.write(base)
    return base


@pytest.fixture(scope="module")
def sources(root):
    return fx.sources_for(root)


@pytest.fixture(scope="module")
def journal(root, sources):
    return Journal(sources, fx.settings_for(root))


def by_id(journal, trade_id):
    trade = journal.tradeset().by_id(trade_id)
    assert trade is not None, f"{trade_id} is missing from the rebuilt trade set"
    return trade


def test_distinct_same_symbol_day_trips_have_distinct_detail_pages(tmp_path):
    import sqlite3
    fx.write(tmp_path)
    src = fx.sources_for(tmp_path)
    with sqlite3.connect(src.state_journal.path) as db:
        for oid, role, side, price, stamp in [('second-entry', 'entry', 'buy', '50', '17:00'),
                                             ('second-exit', 'eod1', 'sell', '51', '18:00')]:
            db.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (oid, '10', fx.CLEAN_DAY, 'AAPL', role, side, '10', price,
                 f'{fx.CLEAN_DAY}T{stamp}:00Z', '0.035', '0.10'))
    j = Journal(src, fx.settings_for(tmp_path))
    first = j.trade(f'{fx.CLEAN_DAY}:AAPL')
    second = j.trade(f'{fx.CLEAN_DAY}:AAPL:2')
    assert first is not None and second is not None
    assert first['trade']['entry_time'] != second['trade']['entry_time']
    assert second['trade']['loss_class'] == 'unknown'
    assert second['trade']['r'] is None
    assert {f['order_id'] for f in second['fills']} == {'second-entry', 'second-exit'}


# ---------------------------------------------------------------------------
# grouping: one trade is a position lifecycle, not one order
# ---------------------------------------------------------------------------

def test_grouping_agrees_with_the_bots_own_reconciler(sources, journal):
    """The dashboard and orb_iex.reconcile must not disagree about what a trade is."""
    plans = {(p["day"], p["symbol"]): D(p["one_r_per_share"]) for p in sources.trade_journal.plans()}
    closed, opened = reconstruct(commission=fx.COMMISSION, slippage=fx.SLIPPAGE,
                                 risk_per_share=plans,
                                 fills=fills_from_journal(sources.state_journal.fills()))
    reference = {(t["symbol"], t["day"]): t for t in closed + opened}
    rebuilt = {(t.symbol, t.day): t for t in journal.tradeset().trades + journal.tradeset().open_trades}
    planned = {(symbol, day) for day, symbol in plans}

    assert set(reference) == set(rebuilt)
    for key, expected in reference.items():
        actual = rebuilt[key]
        assert actual.raw["qty"] == expected["qty"], key
        assert actual.raw["open_qty"] == expected["open_qty"], key
        assert actual.raw["entry_vwap"] == expected["entry_vwap"], key
        assert actual.raw["exit_vwap"] == expected["exit_vwap"], key
        if key in planned:
            assert actual.raw["initial_risk"] == expected["initial_risk"], key
        else:
            # No plan to freeze: reconcile has nothing to compute risk from, while the
            # dashboard uses the documented STOP_ATR_FRACTION x ATR fallback and says so.                assert expected["initial_risk"] is None
                # The documented fallback is STOP_ATR_FRACTION x ATR from the engine trade row;
                # with no plan and no ATR either, R stays unavailable rather than invented.
                assert actual.r_source in ("atr_constant", "unavailable"), key
                assert actual.raw["r"] is None, key
        if expected["exit_vwap"] is None:
            # reconcile reports an unrealized 0 placeholder; the dashboard reports nothing,
            # because a position that has not closed has no realized P&L to report.
            assert actual.realized is None and actual.net is None and actual.r is None, key
        else:
            assert actual.raw["gross_pnl"] == expected["gross_pnl"], key


def test_partial_fills_are_one_trade_with_every_increment_preserved(journal):
    aapl = by_id(journal, f"{fx.CLEAN_DAY}:AAPL")
    assert len(aapl.raw["entry_fills"]) == 2
    assert aapl.raw["entry_vwap"] == D("50.09")         # (60*50.05 + 40*50.15) / 100
    assert aapl.raw["qty"] == D(100) and len(journal.tradeset().trades) == fx.EXPECTED["closed_trades"]
    assert sorted(aapl.raw["order_ids"]) == ["o-aapl-entry", "o-aapl-stop"]
    assert trades_mod.expand(aapl.raw)["entry_fills"][1]["price"] == "50.15"


def test_carry_over_is_one_trade_across_days(journal):
    iwm = by_id(journal, f"{fx.CARRY_DAY}:IWM")
    assert iwm.day == fx.CARRY_DAY and iwm.raw["exit_time"].startswith(fx.NO_PLAN_DAY)
    assert iwm.raw["carry"] is True and "carry_over" in iwm.execution_events
    carry = [q for q in journal.investigation() if "session boundary" in q["title"]]
    assert carry and carry[0]["trade_ids"] == [iwm.trade_id]


def test_open_position_is_reported_and_never_counted_as_a_zero(journal):
    tradeset = journal.tradeset()
    assert [t.trade_id for t in tradeset.open_trades] == [f"{fx.OPEN_DAY}:AMD"]
    amd = tradeset.open_trades[0]
    assert amd.realized is None and amd.net is None and amd.r is None
    assert len(tradeset.trades) == fx.EXPECTED["closed_trades"]
    assert "2026-10-09:AMD" not in {t.trade_id for t in tradeset.trades}
    # It still has to appear in the safety picture, where an unprotected fill belongs.
    unprotected = [q for q in journal.investigation() if "stop coverage" in q["title"]]
    assert unprotected and unprotected[0]["trade_ids"] == [amd.trade_id]


def test_fill_rows_without_a_price_are_dropped_and_reported(sources, journal):
    assert journal.tradeset().dropped_fill_rows == 1
    assert any("carried no price or quantity" in n for n in journal.tradeset().notes)
    # ... and the position size is the priced quantity only, not a zero-priced addition.
    assert by_id(journal, f"{fx.OPEN_DAY}:AMD").raw["qty"] == D(25)


def test_a_flat_position_on_a_session_without_an_engine_trade_row_still_reads_as_closed(journal):
    """Regression: PLTR has no journal.sqlite trades row; the fills are the evidence."""
    funnel = journal.opportunities(fx.NO_PLAN_DAY)
    closed = next(s for s in funnel["stages"] if s["key"] == "closed")
    assert funnel["summary"]["closed"] == 1
    assert (closed["passed"], closed["failed"], closed["unknown"]) == (1, 0, 0)
    assert closed["symbols_passed"] == ["PLTR"]


# ---------------------------------------------------------------------------
# accounting: three views that are never summed together
# ---------------------------------------------------------------------------

def test_realized_modeled_and_net_stay_separate(journal):
    tradeset = journal.tradeset()
    realized = sum((t.realized for t in tradeset.trades), D(0))
    modeled = sum((t.raw["modeled_cost"] for t in tradeset.trades), D(0))
    net = sum((t.raw["net_modeled_pnl"] for t in tradeset.trades), D(0))
    assert str(realized.quantize(D("0.01"))) == fx.EXPECTED["realized_pnl"]
    assert str(modeled.quantize(D("0.01"))) == fx.EXPECTED["modeled_cost"]
    assert str(net.quantize(D("0.01"))) == fx.EXPECTED["modeled_net_pnl"]
    assert net == realized - modeled              # the only relationship between them

    aapl = by_id(journal, f"{fx.CLEAN_DAY}:AAPL")
    # 0.0135/share on the entry (100) and the exit (100) - the modeled cost is on top of
    # the fill prices, and the real slippage inside those prices is never added again.
    assert aapl.raw["modeled_cost"] == D("0.0135") * 200
    assert aapl.raw["gross_pnl"] == (D("50.95") - D("50.09")) * 100
    assert aapl.realized == aapl.raw["gross_pnl"]


def test_recorded_fees_are_null_not_zero(journal):
    row = journal.trades(Filters())["rows"][0]
    assert row["recorded_fees"] is None
    assert row["recorded_fees_note"]
    assert journal.meta()["accounting_rules"]["recorded_fees"].endswith("null, never zero")


def test_slippage_is_measured_against_the_planned_trigger(journal):
    assert by_id(journal, f"{fx.CLEAN_DAY}:AAPL").entry_slippage == D("50.09") - D("50.10")
    # A short filled above its trigger paid slippage: signed in the trade's favour.
    assert by_id(journal, f"{fx.DEFECT_DAY}:MSFT").entry_slippage == D("199.95") - D("200.00")
    assert by_id(journal, f"{fx.NO_PLAN_DAY}:PLTR").entry_slippage is None   # no plan, no measurement


def test_r_uses_frozen_plan_risk_not_the_later_stop(journal):
    msft = by_id(journal, f"{fx.DEFECT_DAY}:MSFT")
    assert msft.one_r_per_share == D("1.00") and msft.r_source == "plan"
    assert msft.final_stop == D("200.95")                  # the stop that was in force at the end
    assert msft.planned_stop == D("200.95")
    assert msft.raw["initial_risk"] == D(50)               # 1.00 x 50 shares, frozen at plan time
    assert msft.r == D(-1)                                 # -50 / 50, not (-50) / (200.95-200)*50

    for trade_id, expected in fx.EXPECTED["r_values"].items():
        assert str(by_id(journal, trade_id).r.quantize(D("0.01"))) == expected, trade_id


def test_a_missing_plan_is_reported_as_missing_never_as_zero(journal):
    pltr = by_id(journal, f"{fx.NO_PLAN_DAY}:PLTR")
    assert pltr.plan_source == "unavailable" and pltr.r_source == "unavailable"
    assert pltr.r is None and pltr.one_r_per_share is None
    assert "no_plan" in pltr.record_gaps and "incomplete_round_trip" in pltr.record_gaps
    assert pltr.realized == D("-4.00")                     # the P&L is known even when R is not

    perf = journal.overview(as_of=fx.DEFECT_DAY)["periods"]["all_time"]
    assert perf["r_trades"] == 3 and perf["r_unavailable"] == fx.EXPECTED["trades_without_r"]


# ---------------------------------------------------------------------------
# classification: strategy blame vs execution blame
# ---------------------------------------------------------------------------

def test_loss_classes_separate_execution_from_strategy(journal):
    assert by_id(journal, f"{fx.CLEAN_DAY}:AAPL").loss_class == "normal_strategy"
    assert by_id(journal, f"{fx.CARRY_DAY}:IWM").loss_class == "normal_strategy"
    # Record gaps are a documentation problem, not a reason to blame execution.
    assert by_id(journal, f"{fx.NO_PLAN_DAY}:PLTR").loss_class == "normal_strategy"

    msft = by_id(journal, f"{fx.DEFECT_DAY}:MSFT")
    assert msft.loss_class == "mixed"                      # execution defect AND diverged bar data
    assert set(msft.execution_defects) == {"flatten_without_stop", "stop_rejected", "exit_late"}
    assert set(msft.data_flags) == {"bar_diverged", "quote_stale"}
    assert msft.broker_flags == ["order_rejected"]

    amd = journal.tradeset().open_trades[0]
    assert amd.loss_class == "execution_defect"
    assert {"entry_unprotected", "unresolved_intent", "stop_update_failed"} <= set(amd.execution_defects)


def test_every_investigation_item_traces_back_to_a_trade(journal):
    trade_ids = {t.trade_id for t in journal.all_trades()}
    for item in journal.investigation():
        assert set(item["trade_ids"]) <= trade_ids
        assert item["evidence_source"] and item["deterministic"] is True


# ---------------------------------------------------------------------------
# lifecycle: every step carries its evidence
# ---------------------------------------------------------------------------

def test_lifecycle_walks_signal_to_reconciliation_with_raw_evidence(journal):
    payload = journal.trade(f"{fx.DEFECT_DAY}:MSFT")
    steps = {s["step"]: s for s in payload["steps"]}
    assert list(steps) == ["signal", "candidate", "quote", "intent", "order", "ack", "fills",
                           "partial_fill_handling", "protection", "stop_changes", "exit",
                           "reconciliation"]
    assert steps["signal"]["state"] == "recorded" and steps["signal"]["evidence"]["row"]
    assert steps["quote"]["state"] == "recorded"           # the log has the pre-submit quote
    assert steps["stop_changes"]["state"] == "recorded"    # a replaced stop IS a stop change
    assert steps["exit"]["detail"]["exit_reason"] == "flatten"
    assert steps["reconciliation"]["state"] == "balanced"
    # The raw evidence is preserved underneath: orders, fills and the append-only events.
    assert {o["order_id"] for o in payload["orders"]} == {"o-msft-entry", "o-msft-stop-1",
                                                          "o-msft-stop-2", "o-msft-flatten"}
    assert len(payload["fills"]) == 3 and payload["exec_events"][0]["kind"] == "stop_failed"
    assert [e["event"] for e in payload["log_events"]].count("order_rejected") == 1


def test_lifecycle_reports_an_unavailable_step_as_unavailable(journal):
    payload = journal.trade(f"{fx.OPEN_DAY}:AMD")
    steps = {s["step"]: s for s in payload["steps"]}
    assert steps["intent"]["state"] == "recorded"          # the unresolved stop intent is on record
    assert steps["protection"]["state"] == "missing"      # no stop order was ever recorded
    assert "entry_unprotected" in payload["trade"]["execution_defects"]
    assert steps["exit"]["state"] == "still_open"
    assert steps["reconciliation"]["state"] == "imbalanced"


def test_unknown_trade_id_returns_nothing(journal):
    assert journal.trade("2026-01-01:NOPE") is None


# ---------------------------------------------------------------------------
# funnel: why didn't it trade
# ---------------------------------------------------------------------------

def test_a_healthy_no_trade_day_is_distinguishable_from_a_bot_failure(journal):
    flat = journal.opportunities(fx.FLAT_DAY)
    assert flat["summary"]["diagnosis"]["state"] == "healthy_no_trade"
    assert flat["scan_ran"] is True and flat["summary"]["attempted"] == 2
    eligibility = next(s for s in flat["stages"] if s["key"] == "eligibility")
    assert (eligibility["entered"], eligibility["passed"], eligibility["failed"]) == (2, 0, 2)
    assert eligibility["reasons"] == {"relvol<1": 1, "atr<=0.50": 1}
    assert {e["symbol"] for e in eligibility["symbols_failed"]} == {"KO", "PEP"}

    failed = journal.opportunities(fx.SCAN_FAILED_DAY)
    assert failed["scan_ran"] is False
    assert failed["summary"]["diagnosis"]["state"] == "not_run"
    assert "bot/session issue" in failed["summary"]["diagnosis"]["detail"]

    empty = journal.opportunities(fx.NO_DATA_DAY)
    assert empty["summary"]["diagnosis"]["state"] == "unavailable"
    assert empty["rules_source"] == "none"


def test_funnel_reports_both_the_journaled_and_the_reconstructed_checklist(journal):
    assert journal.opportunities(fx.CLEAN_DAY)["rules_source"] == "journaled"
    rebuilt = journal.opportunities(fx.NO_PLAN_DAY)
    assert rebuilt["rules_source"] == "reconstructed"
    assert any("re-derived with the very same rule_checklist function" in n for n in rebuilt["notes"])


def test_every_funnel_count_has_symbols_behind_it(journal):
    funnel = journal.opportunities(fx.DEFECT_DAY)
    summary = funnel["summary"]
    assert (summary["attempted"], summary["signals"], summary["submitted"]) == (3, 1, 1)
    assert summary["filled"] == 1 and summary["protected"] == 1 and summary["closed"] == 1

    eligibility = next(s for s in funnel["stages"] if s["key"] == "eligibility")
    assert eligibility["reasons"] == {"adv<1M": 1}
    assert [e["symbol"] for e in eligibility["symbols_failed"]] == ["NVDA"]
    ranking = next(s for s in funnel["stages"] if s["key"] == "ranking")
    assert [e["symbol"] for e in ranking["symbols_failed"]] == ["ORCL"]
    submission = next(s for s in funnel["stages"] if s["key"] == "submission")
    assert submission["symbols_passed"] == ["MSFT"]


def test_funnel_shows_an_unprotected_fill_as_unprotected(journal):
    funnel = journal.opportunities(fx.OPEN_DAY)
    protected = next(s for s in funnel["stages"] if s["key"] == "protected")
    closed = next(s for s in funnel["stages"] if s["key"] == "closed")
    assert (funnel["summary"]["filled"], funnel["summary"]["protected"]) == (1, 0)
    assert protected["symbols_failed"] and protected["symbols_failed"][0]["symbol"] == "AMD"
    assert closed["symbols_failed"] and closed["symbols_failed"][0]["symbol"] == "AMD"
    # GOOG was filtered out before the quote gate, which is reported as unknown, not as a pass.
    eligibility = next(s for s in funnel["stages"] if s["key"] == "eligibility")
    assert "GOOG" in eligibility["symbols_passed"]


def test_the_funnel_opens_on_the_newest_session_that_has_candidates(journal):
    """A pre-created session row with no candidates must not be the landing page."""
    assert journal.day_list()[0] == fx.NO_DATA_DAY          # newest day, no candidates at all
    payload = journal.opportunities()                       # no explicit day
    assert payload["selected"] == fx.OPEN_DAY
    assert payload["summary"]["attempted"] == 2
    assert journal.opportunities(fx.NO_DATA_DAY)["selected"] == fx.NO_DATA_DAY


def test_funnel_notes_when_there_are_no_candidate_rows(journal):
    empty = journal.opportunities(fx.NO_DATA_DAY)
    assert empty["summary"]["attempted"] == 0
    assert any("funnel is unknown, not empty" in n for n in empty["notes"])


# ---------------------------------------------------------------------------
# calendar and reviews
# ---------------------------------------------------------------------------

def test_calendar_day_states(journal):
    calendar = journal.calendar(fx.MONTH)["calendar"]
    cells = {c["day"]: c for c in calendar["cells"]}
    assert len(calendar["cells"]) == 31
    assert cells[fx.CLEAN_DAY]["state"] == "traded"
    assert cells[fx.OPEN_DAY]["state"] == "traded"          # filled and still open at the close
    assert cells[fx.FLAT_DAY]["state"] == "healthy_no_trades"
    assert cells[fx.SCAN_FAILED_DAY]["state"] == "not_run"
    assert cells[fx.MARKET_CLOSED_DAY]["state"] == "market_closed"
    assert cells["2026-10-15"]["state"] == "unavailable"
    assert calendar["totals"] == {"sessions_recorded": 8, "traded": 5, "healthy_no_trades": 2,
                                  "not_run": 1, "market_closed": 9, "unavailable": 14, "trades": 4,
                                  "realized_pnl": "42.00", "modeled_net_pnl": "36.60", "incidents": 1}
    # One trade with three defects is one incident trade, not three incidents.
    assert cells[fx.DEFECT_DAY]["incidents"] == 1
    assert cells[fx.DEFECT_DAY]["net_r"] == "-1.00"


def test_reviews_carry_sample_size_and_record_gaps(journal):
    reviews = journal.reviews()
    daily = reviews["daily"]
    assert daily["label"] == fx.DEFECT_DAY and daily["trading_days"] == 1
    assert daily["metrics"]["valid"] is False
    assert any("sample of one-ish" in n for n in daily["notes"])
    all_time = reviews["all_time"]
    assert any("no frozen initial risk" in n for n in all_time["notes"])
    # The review window covers the days that traded, so the rollup counts those only.
    assert all_time["funnel"]["states"] == {"traded": 4}
    assert all_time["worst_strategy_trades"][0]["trade_id"] == f"{fx.NO_PLAN_DAY}:PLTR"
    assert all_time["best_strategy_trades"][0]["trade_id"] == f"{fx.CLEAN_DAY}:AAPL"
    slippages = [D(r["entry_slippage_per_share"]) for r in all_time["largest_slippage"]]
    assert slippages == sorted(slippages, reverse=True)    # signed: positive is the adverse side
    assert all_time["largest_slippage"][0]["trade_id"] == f"{fx.CLEAN_DAY}:AAPL"
    assert all_time["day_summaries"] == []


# ---------------------------------------------------------------------------
# paper vs backtest
# ---------------------------------------------------------------------------

def test_comparison_compares_only_what_is_comparable(journal):
    payload = journal.comparison()
    assert payload["backtest"]["models"]                       # the research artifact is read
    assert payload["backtest"]["reference_model"] == "calibrated/paper ALL"
    assert payload["expectancy"]["backtest_expectancy_r"] == "0.11"
    assert payload["expectancy"]["valid"] is False             # 4 paper trades prove nothing
    assert payload["paper"]["trades"] == 4
    # The research artifact is aggregate, so per-day alignment is declared unavailable with
    # the exact artifact the run would have to produce. It is never mocked up.
    for key in ("matching_dates", "matched_candidates", "unmatched_candidates", "signal_mismatch",
                "fill_no_fill_mismatch", "aligned_equity_curve"):
        assert payload["alignment"][key]["available"] is False
        assert payload["alignment"][key]["reason"]
    assert "per-session return series" in payload["alignment"]["matching_dates"]["data_needed"]
    assert "do not prove live tradability" in payload["banner"]
    assert payload["costs"]["paper_modeled_cost_total"] == "5.40"
    assert payload["slippage"]["expected_per_share_model"] == "0.01"


def test_comparison_reports_the_residual_it_cannot_decompose(journal):
    residual = journal.comparison()["residual"]
    assert residual["realized_vs_backtest"]["paper_modeled_net_pnl"] == "36.60"
    assert residual["realized_vs_backtest"]["unexplained"] is not None
    assert "cannot be split into signal, sizing, fill and cost components" in \
           residual["realized_vs_backtest"]["reason"]
    assert residual["paper_trades_vs_backtest_trades"]["backtest"] == 4210


# ---------------------------------------------------------------------------
# filters and the trade table
# ---------------------------------------------------------------------------

def test_filters_select_and_paginate(journal):
    all_rows = journal.trades(Filters())
    assert all_rows["total"] == 4 and all_rows["returned"] == 4
    assert not any(r["open_qty"] != "0" for r in all_rows["rows"])

    assert journal.trades(Filters(symbol="msft"))["total"] == 1
    assert journal.trades(Filters(side="short"))["total"] == 2
    assert journal.trades(Filters(loss_class="mixed"))["total"] == 1
    assert journal.trades(Filters(defect=True))["total"] == 1
    assert journal.trades(Filters(defect=False))["total"] == 3
    assert journal.trades(Filters(exit_reason="eod"))["total"] == 2
    assert journal.trades(Filters(day_from=fx.CLEAN_DAY))["total"] == 2
    assert journal.trades(Filters(search="pltr"))["total"] == 1
    assert journal.trades(Filters(min_net=D("0")))["total"] == 2
    assert journal.trades(Filters(strategy_version="config-7"))["total"] == 4
    only_open = journal.trades(Filters(only_open=True))
    assert only_open["total"] == 1 and only_open["rows"][0]["trade_id"] == f"{fx.OPEN_DAY}:AMD"

    page = journal.trades(Filters(sort="modeled_net_pnl", order="asc", limit=2))
    assert page["total"] == 4 and page["returned"] == 2 and page["limit"] == 2
    assert [r["trade_id"] for r in page["rows"]] == [f"{fx.DEFECT_DAY}:MSFT", f"{fx.NO_PLAN_DAY}:PLTR"]
    assert journal.trades(Filters(limit=2, offset=2))["returned"] == 2
    assert page["filters"] == [] or page["filters"][0]["name"] in {"sort", "order", "min_net"}


def test_filter_query_parsing_is_defensive():
    filters = Filters.from_query({"symbol": ["aapl"], "limit": ["99999"], "offset": ["-3"],
                                  "min_net": ["not-a-number"], "only_open": ["yes"]})
    assert filters.symbol == "aapl" and filters.limit == 5000 and filters.offset == 0
    assert filters.min_net is None and filters.only_open is True
    assert Filters.from_query({}).limit == 500


def test_trade_rows_carry_the_execution_flags_the_table_shows(journal):
    row = journal.trades(Filters(symbol="MSFT"))["rows"][0]
    assert row["entry_vwap"] == "200.00" and row["exit_vwap"] == "201.00"
    assert row["planned_entry"] == "199.95" and row["planned_stop"] == "200.95"
    assert row["realized_pnl"] == "-50.00" and row["modeled_net_pnl"] == "-51.35"
    assert row["entry_fill_count"] == 1 and row["exit_fill_count"] == 2
    assert set(row["execution_flags"]) == set(row["execution_defects"]) | set(row["execution_events"])
    assert row["holding_seconds"] == 655                    # 13:35:05Z -> 13:46:00Z
    assert row["mfe_mae_source"] == "fill_prices_only"


# ---------------------------------------------------------------------------
# meta and snapshot
# ---------------------------------------------------------------------------

def test_meta_declares_read_only_and_the_accounting_rules(journal):
    meta = journal.meta()
    assert meta["read_only"] is True and meta["mode"] == "paper"
    assert "cannot place, cancel or replace an order" in meta["read_only_note"]
    assert set(meta["accounting_rules"]) >= {"realized_pnl", "modeled_cost", "modeled_net_pnl", "r"}
    assert "modeled slippage is never added" in meta["accounting_rules"]["never_added"]
    # No credential-shaped value ever reaches a page: only the allowlisted config keys exist.
    assert set(meta["config"]) <= set(__import__("orb_iex.dashboard.sources", fromlist=["x"]).CONFIG_KEYS) \
        | {"source"}
    assert meta["availability"]["state_journal"]["state"] == "ok"
    assert meta["availability"]["trade_journal"]["required_tables_missing"] == []


def test_snapshot_is_one_serializable_document(journal):
    snapshot = journal.snapshot()
    assert set(snapshot) == {"meta", "overview", "trades", "execution", "opportunities", "analytics",
                            "comparison", "calendar", "reviews", "mistakes", "improvement"}
    blob = json.dumps(snapshot, default=str)
    assert len(blob) > 10_000
    assert json.loads(blob)["trades"]["total"] == 4


def test_the_trade_journal_read_is_the_file_the_bot_writes(root):
    """One naming rule for the writer and the reader.

    The bot's own settings default to runtime.mode = "readonly", which is not a journal it
    writes, so a dashboard that took its mode from there would report the paper journal as
    missing and silently degrade to reconstructed funnels. ``mode`` must come from the caller.
    """
    from orb_iex.cli import _tradelog_paths
    from orb_iex.config import parse_settings
    from orb_iex.dashboard.sources import Sources

    data_dir = root / "runtime"
    settings = parse_settings({"runtime": {"data_dir": str(data_dir), "mode": "readonly"},
                               "journal": {"dir": str(data_dir / "journal")}})
    for mode in ("paper", "shadow", "signal"):
        expected, _ = _tradelog_paths(settings, mode)
        assert Sources.from_settings(settings, root=root, mode=mode).trade_journal.path == expected
        assert (data_dir / "journal" / expected.name).name == expected.name

    default = Sources.from_settings(settings, root=root)
    assert default.mode == "readonly"                       # settings.mode is not disguised
    assert default.trade_journal.path.name == "trade_journal_readonly.sqlite"
    assert default.trade_journal.integrity()["state"] != "ok"   # and it says so out loud

    paper = Sources.from_settings(settings, root=root, mode="paper")
    assert paper.trade_journal.integrity()["state"] == "ok"
    assert len(paper.trade_journal.plans()) == len(fx.TLOG_PLANS)


def test_the_dashboard_cli_reads_the_mode_it_is_asked_for(root):
    """`journal-snapshot --mode paper` must open the paper journal, not settings.runtime.mode."""
    from orb_iex.cli import _journal_dashboard

    config = root / "settings.toml"
    config.write_text(f'[runtime]\nmode = "readonly"\ndata_dir = "{root / "runtime"}"\n'
                      f'[journal]\ndir = "{root / "runtime" / "journal"}"\n')
    args = type("Args", (), {"config": str(config), "mode": "paper"})()
    journal, settings, sources = _journal_dashboard(args)
    assert settings.mode == "readonly" and sources.mode == "paper"
    assert sources.trade_journal.path.name == "trade_journal.sqlite"
    assert sources.available()["trade_journal"]["state"] == "ok"
    assert journal.meta()["mode"] == "paper"


def test_overview_status_strip_reports_the_open_risk(journal):
    status = journal.overview(as_of=fx.OPEN_DAY)["status"]
    assert status["mode"] == "paper"
    assert any(b["kind"] == "unprotected" and b["symbol"] == "AMD" for b in status["breakers"])
    assert status["unresolved_intents"] >= 1
    assert status["ratios_valid"] is False and "at least 20" in status["ratios_reason"]
    positions = journal.overview(as_of=fx.OPEN_DAY)["positions"]
    assert positions["open_count"] == 1
    assert positions["journal_owned_qty"] == {"AMD": "25.0"}
    assert positions["broker_snapshot_positions"] == {"AMD": 25}
