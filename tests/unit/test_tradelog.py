"""Trading journal: append-only storage, candidate/skip records, rule checklist,
outcome math (MFE/MAE, R), partial fills, restart recovery, summaries, exports."""

import csv
import json
import queue
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from helpers import SYMBOLS, FakeData, Harness, bar, calendar, et, settings
from orb_iex import cli
from orb_iex.clock import FakeClock
from orb_iex.engine import Engine
from orb_iex.journal import Journal
from orb_iex.runner import Runner
from orb_iex.sim import SimBroker
from orb_iex.tradelog import (DEVIATION_TAGS, StrategyChangeLog, TradeLog, build_outcomes, export_all,
                              export_day, max_drawdown, mfe_mae, r_multiple, summarize)

DAY = date(2026, 9, 24)


def tl(tmp_path, name="tj.sqlite"):
    t = TradeLog(tmp_path / name, tmp_path / "exports")
    t.run_id = "test-run"
    return t


# --- append-only ---------------------------------------------------------------

def test_journal_tables_are_append_only(tmp_path):
    t = tl(tmp_path)
    t.candidate(DAY, {"symbol": "AAA", "rank": 1, "action": "trade"})
    t.event(DAY, "fill", "AAA", dedupe_key="fill:x:10", qty=D(10))
    t.plan(DAY, {"symbol": "AAA", "planned_qty": 10})
    t.outcome(DAY, {"symbol": "AAA", "status": "closed"})
    t.save_day_summary(DAY, {"trades": 1})
    t._insert("runs", {"run_id": "r1", "mode": "paper"})
    for table in ("candidates", "exec_events", "runs", "plans", "outcomes", "day_summaries"):
        with pytest.raises(sqlite3.DatabaseError):
            t.db.execute(f"UPDATE {table} SET run_id='x'")
        with pytest.raises(sqlite3.DatabaseError):
            t.db.execute(f"DELETE FROM {table}")
    # a repeated record with the same natural key is ignored, never overwritten
    t.candidate(DAY, {"symbol": "AAA", "rank": 99, "action": "skip"})
    assert t.event(DAY, "fill", "AAA", dedupe_key="fill:x:10", qty=D(99)) is False
    assert [r["rank"] for r in t.rows("candidates")] == [1]
    assert [r["qty"] for r in t.rows("exec_events")] == ["10"]


def test_strategy_change_log_is_append_only_and_cli(tmp_path, capsys):
    cfg = tmp_path / "s.toml"
    cfg.write_text(f'[runtime]\ndata_dir = "{tmp_path}/rt"\n[journal]\ndir = "{tmp_path}/jr"\n')
    assert cli.main(["--config", str(cfg), "changelog", "add", "--rule", "max_gross_exposure", "--old", "1",
                     "--new", "4", "--reason", "paper says 4x", "--evidence", "163 tests; replay 2026-09-24",
                     "--commit", "859a56b", "--date", "2026-09-25"]) == 0
    assert cli.main(["--config", str(cfg), "changelog", "list"]) == 0
    assert "max_gross_exposure: 1 -> 4" in capsys.readouterr().out
    log = StrategyChangeLog(tmp_path / "jr" / "strategy_changes.sqlite")
    assert log.entries()[0]["test_evidence"] == "163 tests; replay 2026-09-24"
    with pytest.raises(sqlite3.DatabaseError):
        log.db.execute("UPDATE strategy_changes SET new_value='9'")
    with pytest.raises(sqlite3.DatabaseError):
        log.db.execute("DELETE FROM strategy_changes")
    assert cli.main(["--config", str(cfg), "changelog", "add", "--rule", "x"]) == cli.EXIT_CONFIG


# --- outcome math ------------------------------------------------------------------

def test_mfe_mae_long_and_short():
    assert mfe_mae(1, D("50"), D("50.8"), D("49.7")) == (D("0.8"), D("0.3"))
    assert mfe_mae(-1, D("50"), D("50.4"), D("49.1")) == (D("0.9"), D("0.4"))
    assert mfe_mae(1, D("50"), D("49.9"), D("49.5")) == (D("0"), D("0.5"))     # never in profit


def test_r_multiple():
    assert r_multiple(D("-0.20"), D("0.20")) == D("-1")
    assert r_multiple(D("0.50"), D("0.20")) == D("2.5")
    assert r_multiple(D("1"), D("0")) is None


def test_max_drawdown():
    assert max_drawdown([D(10), D(-5), D(-10), D(20), D(-3)]) == D(15)
    assert max_drawdown([]) == D(0)


# --- engine-level trade records ------------------------------------------------------

def harness_with_log(tmp_path, **kw):
    h = Harness(tmp_path, **kw)
    h.tlog = tl(tmp_path)
    h.engine.tlog = h.tlog
    h.open_session()
    return h


def plan_row(h, symbol="AAA", direction=1, qty=100, entry="50.00", stop="49.80"):
    h.tlog.plan(h.day, {"symbol": symbol, "direction": direction, "entry_trigger": D(entry), "planned_stop": D(stop),
                        "planned_qty": qty, "planned_exposure": D(entry) * qty,
                        "risk_dollars": abs(D(entry) - D(stop)) * qty, "risk_pct": D("0.02"),
                        "one_r_per_share": abs(D(entry) - D(stop)), "one_r_dollars": abs(D(entry) - D(stop)) * qty,
                        "sizing": {"binding": "risk"}})
    h.tlog.candidate(h.day, {"symbol": symbol, "rank": 1, "direction": direction, "relvol": D("2.5"),
                             "action": "trade"})


def minute_bars(symbol, hi, lo, start=(9, 36), n=30):
    t = et(DAY, *start)
    return {symbol: [bar(t + timedelta(minutes=i), 50, hi, lo, 50, 100) for i in range(n)]}


def test_stopped_trade_record_mfe_mae_and_r(tmp_path):
    h = harness_with_log(tmp_path)
    plan_row(h)
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D("50.00"))
    h.pump()
    h.clock.advance(600)
    stop = h.engine.current_stop(h.trade())
    h.sim.fill(stop.order_id, D(100), D("49.78"))                 # stop slipped 2 cents
    h.pump()
    bars = minute_bars("AAA", "50.50", "49.75", start=(9, 30), n=20)
    rows = build_outcomes(h.j, h.tlog, DAY, h.settings, bars, h.schedule.scan)
    o = rows[0]
    assert o["status"] == "closed" and o["exit_reason"] == "stop"
    assert o["entry_price"] == "50.0000" and o["exit_price"] == "49.7800"
    assert o["gross_pnl"] == "-22.00" and o["realized_r"] == "-1.10"
    assert o["exit_slippage_per_share"] == "0.0200"
    assert o["mfe_per_share"] == "0.5000" and o["mfe_r"] == "2.50"
    assert o["mae_per_share"] == "0.2500" and o["mae_r"] == "1.25"
    assert o["modeled_commission"] == "0.7000" and o["net_pnl_modeled"] == "-24.70"
    assert o["holding_seconds"] == 600
    assert o["rules_followed"] == 1 and o["execution_clean"] == 1 and o["data_complete"] == 1
    assert set(o["deviation_tags"]) <= set(DEVIATION_TAGS)
    stored = h.tlog.rows("outcomes")[0]
    assert stored["realized_r"] == "-1.10" and json.loads(stored["deviation_tags"]) == []
    kinds = [e["kind"] for e in h.tlog.events(DAY.isoformat(), "AAA")]
    assert kinds.count("submit") == 2 and kinds.count("fill") == 2 and "status" in kinds


def test_partial_fills_and_stop_replacement_are_recorded(tmp_path):
    h = harness_with_log(tmp_path, now=et(DAY, 9, 35, 5))
    plan_row(h)
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(30), D("50.00"))
    h.pump()
    h.sim.fill(t.entry_order_id, D(70), D("50.10"))
    h.pump()
    h.clock.set(et(DAY, 15, 59))
    h.engine.begin_eod(h.clock.now())
    h.pump()
    h.sim.last_price["AAA"] = D("50.40")
    h.engine.tick(h.clock.now())
    eod = [o for o in h.j.orders(DAY.isoformat(), "AAA") if o.role == "eod"][0]
    h.sim.fill(eod.order_id, D(100), D("50.40"))
    h.pump()
    o = build_outcomes(h.j, h.tlog, DAY, h.settings, minute_bars("AAA", "50.6", "49.9"), h.schedule.scan)[0]
    assert o["filled_qty"] == "100" and o["entry_price"] == "50.0700"
    assert {"PARTIAL_FILL", "STOP_REPLACED", "EOD_EXIT"} <= set(o["deviation_tags"])
    assert o["exit_reason"] == "eod" and o["execution_clean"] == 0
    fills = [e for e in h.tlog.events(DAY.isoformat(), "AAA") if e["kind"] == "fill" and e["role"] == "entry"]
    assert [e["qty"] for e in fills] == ["30", "70"]
    assert any(e["kind"] == "stop_update" for e in h.tlog.events(DAY.isoformat(), "AAA"))


def test_restart_recovery_is_tagged_and_events_not_duplicated(tmp_path):
    h = harness_with_log(tmp_path)
    plan_row(h)
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D("50.00"))
    h.pump()
    before = len(h.tlog.rows("exec_events"))
    # restart: new engine + new run id on the same journal
    h.tlog.run_id = "test-run-2"
    h.clock.set(et(DAY, 12, 0))
    e2 = Engine(h.j, h.sim, h.settings, h.clock, "paper")
    e2.tlog = h.tlog
    e2.start_day(h.schedule)
    e2.recover(h.clock.now())
    e2.reconcile(h.clock.now(), side_effects=True)                # same fills seen again
    events = h.tlog.rows("exec_events")
    assert [e["kind"] for e in events[before:]] == ["recovery"]
    stop = e2.current_stop(h.j.trade(DAY.isoformat(), "AAA"))
    h.sim.fill(stop.order_id, D(100), D("49.80"))
    while not h.q.empty():
        e2.on_update(h.q.get())
    o = build_outcomes(h.j, h.tlog, DAY, h.settings, minute_bars("AAA", "50.2", "49.8"), h.schedule.scan)[0]
    assert "RECOVERY_EVENT" in o["deviation_tags"] and o["execution_clean"] == 0
    assert o["realized_r"] == "-1.00"
    # outcomes are written once: building again adds nothing
    build_outcomes(h.j, h.tlog, DAY, h.settings, None, h.schedule.scan)
    assert len(h.tlog.rows("outcomes")) == 1


def test_duplicate_fill_message_is_tagged(tmp_path):
    h = harness_with_log(tmp_path)
    plan_row(h)
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D("50.00"))
    msgs = []
    while not h.q.empty():
        msgs.append(h.q.get())
    for m in msgs + [m for m in msgs if m.event == "fill"]:
        h.engine.on_update(m)
    assert any(e["kind"] == "duplicate" for e in h.tlog.events(DAY.isoformat(), "AAA"))


def test_manual_cancel_of_stop_is_tagged(tmp_path):
    h = harness_with_log(tmp_path)
    plan_row(h)
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D("50.00"))
    h.pump()
    h.sim.last_price["AAA"] = D("49.90")
    h.sim.auto_fill_market = True
    h.sim.cancel_order(h.engine.current_stop(h.trade()).order_id)   # someone cancels the stop by hand
    h.pump()
    o = build_outcomes(h.j, h.tlog, DAY, h.settings, minute_bars("AAA", "50.1", "49.9"), h.schedule.scan)[0]
    assert "MANUAL_INTERFERENCE" in o["deviation_tags"]
    assert o["rules_followed"] == 0 and o["exit_reason"] == "stop_failed_exit"


def test_missing_bars_mark_data_incomplete(tmp_path):
    h = harness_with_log(tmp_path)
    plan_row(h)
    t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D("50.00"))
    h.pump()
    h.sim.fill(h.engine.current_stop(h.trade()).order_id, D(100), D("49.80"))
    h.pump()
    o = build_outcomes(h.j, h.tlog, DAY, h.settings, None, h.schedule.scan)[0]
    assert "DATA_MISSING" in o["deviation_tags"] and o["data_complete"] == 0
    assert o["mfe_per_share"] is not None                          # still from the fills themselves


# --- summary math -----------------------------------------------------------------

def out(sym, direction, net, r, relvol, exit_reason="stop", exit_time="2026-09-24T15:00:00+00:00", mfe="1", mae="0.5"):
    return {"symbol": sym, "direction": direction, "gross_pnl": str(D(net) + 1), "net_pnl_modeled": str(net),
            "net_pnl_paper_costs": str(D(net) + D("0.5")), "realized_r": str(r), "relvol": str(relvol),
            "exit_reason": exit_reason, "exit_time": exit_time, "mfe_r": mfe, "mae_r": mae,
            "modeled_commission": "0.5", "modeled_slippage": "0.5", "actual_slippage": "0.2", "status": "closed",
            "rules_followed": 1, "execution_clean": 1, "data_complete": 1, "deviation_tags": "[]"}


def test_summary_math():
    outcomes = [out("A", 1, 30, "3", "1.5", "eod", "2026-09-24T19:59:00+00:00"),
                out("B", 1, -10, "-1", "2.5", exit_time="2026-09-24T14:00:00+00:00"),
                out("C", -1, -10, "-1", "12", exit_time="2026-09-24T15:00:00+00:00"),
                out("D", -1, 20, "2", "4", "eod", "2026-09-24T16:00:00+00:00")]
    cands = [{"symbol": s, "rank": i + 1, "action": "trade"} for i, s in enumerate("ABCD")] + \
        [{"symbol": "E", "rank": 5, "action": "skip", "skip_reason": "not_shortable"},
         {"symbol": "F", "rank": None, "action": "skip", "skip_reason": "relvol<1"},
         {"symbol": "G", "rank": None, "action": "skip", "skip_reason": "relvol<1"}]
    s = summarize(outcomes, cands, "2026-09-24", D(100000))
    assert s["trades"] == 4 and s["wins"] == 2 and s["losses"] == 2 and s["win_rate"] == "0.5000"
    assert s["net_pnl_modeled"] == "30.00" and s["gross_pnl"] == "34.00" and s["cost_drag"] == "4.00"
    assert s["profit_factor"] == "2.50"                      # 50 / 20
    assert s["expectancy_per_trade"] == "7.50" and s["average_r"] == "0.75"
    assert s["average_win"] == "25.00" and s["average_loss"] == "-10.00"
    assert s["stop_outs"] == 2 and s["eod_exits"] == 2
    assert s["skipped_by_reason"] == {"relvol<1": 2, "not_shortable": 1}
    assert s["candidates_scanned"] == 7 and s["candidates_passed_filters"] == 5 and s["candidates_top20"] == 5
    assert s["long"]["net_pnl"] == "20.00" and s["short"]["net_pnl"] == "10.00"
    assert s["by_relvol_bucket"]["1-2"]["trades"] == 1 and s["by_relvol_bucket"]["10+"]["trades"] == 1
    # ordered by exit time: B -10, C -10, D +20, A +30 -> max drawdown 20
    assert s["max_drawdown"] == "20.00" and s["max_drawdown_pct_of_equity"] == "0.0200"
    assert s["average_mfe_r"] == "1.00" and s["average_mae_r"] == "0.50"


def test_no_trade_day_summary():
    s = summarize([], [{"symbol": "X", "rank": None, "action": "skip", "skip_reason": "not_ready:opening_bar"}], "d")
    assert s["trades"] == 0 and s["win_rate"] is None and s["profit_factor"] is None
    assert s["skipped_by_reason"] == {"not_ready:opening_bar": 1}


# --- full day through the runner, CSV export, no key leakage -------------------------

def minute_path(day, symbols):
    out_ = {}
    for i, s in enumerate(symbols):
        bars, t = [], et(day, 9, 35)
        while t < et(day, 16, 0):
            bars.append(bar(t, "50.9", "51.3", "50.85", "51.2", 1000) if i % 2 == 0
                        else bar(t, "49.1", "49.15", "48.7", "48.8", 1000))
            t += timedelta(minutes=1)
        out_[s] = bars
    return out_


def run_day(tmp_path, mode="shadow", data=None):
    cal = calendar()
    clock = FakeClock(et(DAY, 9, 0))
    q = queue.Queue()
    data = data or FakeData(cal, minute=minute_path(DAY, SYMBOLS))
    orig = data.assets

    def assets(day):
        rows = orig(day)
        for a in rows:
            if a["symbol"] == SYMBOLS[23]:
                a["easy_to_borrow"] = False                  # one top-ranked short becomes hard to borrow
        return rows
    data.assets = assets
    j = Journal(tmp_path / f"{mode}.sqlite")
    tlog = TradeLog(tmp_path / "journal" / "trade_journal.sqlite", tmp_path / "journal" / "exports")
    broker = SimBroker(q.put, clock=clock.now) if mode != "signal" else None
    r = Runner(settings(tmp_path), mode, clock, client=None, data=data, journal=j, broker=broker, events=q,
               calendar=cal, feed_prefetch=True, tlog=tlog,
               account_source=lambda: {"equity": "100000", "cash": "100000", "buying_power": "400000",
                                       "shorting_enabled": True})
    r.run(until=et(DAY, 16, 5))
    return tlog, j


def test_full_day_records_candidates_plans_outcomes_and_exports(tmp_path):
    tlog, j = run_day(tmp_path)
    runs = tlog.rows("runs")
    assert len(runs) == 1 and runs[0]["equity"] == "100000" and runs[0]["max_gross_exposure"] == "4"
    assert runs[0]["timezone"] == "America/New_York" and runs[0]["config_version"]
    assert json.loads(runs[0]["feeds"])["opening_feed"] == "iex"
    cands = {c["symbol"]: c for c in tlog.rows("candidates")}
    assert len(cands) == 26                                          # every eligible name, incl. skipped
    htb = cands[SYMBOLS[23]]
    assert htb["action"] == "skip" and htb["skip_reason"] == "not_easy_to_borrow"
    rules = json.loads(htb["rules"])
    assert rules["shortable"] is True and rules["easy_to_borrow"] is False and rules["in_top20"] is True
    below = [c for c in cands.values() if c["skip_reason"] == "rank>20"]
    assert below and json.loads(below[0]["rules"])["in_top20"] is False
    spy = cands["SPY"]
    assert spy["skip_reason"].startswith("not_ready") and json.loads(spy["rules"])["opening_bar_present"] is False
    traded = [c for c in cands.values() if c["action"] == "trade"]
    assert len(traded) == 19
    long_rules = json.loads(cands[SYMBOLS[24]]["rules"])
    assert all(v is True for k, v in long_rules.items() if k not in ("shortable", "easy_to_borrow",
                                                                   "short_account_enabled"))
    assert long_rules["shortable"] is None                          # not applicable to a long
    plans = tlog.rows("plans")
    assert len(plans) == 20 and all(D(p["risk_pct"]) > 0 for p in plans)
    outcomes = tlog.rows("outcomes")
    assert len(outcomes) == 19 and all(o["status"] == "closed" for o in outcomes)
    assert all(o["exit_reason"] in ("stop", "eod") for o in outcomes)
    assert any(e["kind"] == "scan" for e in tlog.rows("exec_events"))
    daily = tmp_path / "journal" / "exports" / "daily"
    with (daily / f"trades_{DAY}.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 19 and "realized_r" in rows[0] and "deviation_tags" in rows[0]
    with (daily / f"candidates_{DAY}.csv").open() as fh:
        assert len(list(csv.DictReader(fh))) == 26
    md = (daily / f"summary_{DAY}.md").read_text()
    assert "| trades | 19 |" in md and "not_easy_to_borrow" in md
    assert (tmp_path / "journal" / "exports" / "weekly" / "summary_2026-W39.md").exists()
    assert (tmp_path / "journal" / "exports" / "monthly" / "summary_2026-09.md").exists()


def test_signal_mode_no_trade_records(tmp_path):
    cal = calendar()
    data = FakeData(cal, missing_today=set(SYMBOLS))                 # no IEX opening bars at all
    tlog, _ = run_day(tmp_path, mode="signal", data=data)
    cands = tlog.rows("candidates")
    assert cands and all(c["action"] == "skip" and c["skip_reason"].startswith("not_ready") for c in cands)
    assert tlog.rows("outcomes") == []
    summary = json.loads((tmp_path / "journal" / "exports" / "daily" / f"summary_{DAY}.json").read_text())
    assert summary["trades"] == 0 and summary["skipped_by_reason"]


def test_journal_never_contains_api_keys(tmp_path, monkeypatch):
    key, secret = "PKLEAKTEST123456", "leak-secret-value-abcdef"
    monkeypatch.setenv("APCA_API_KEY_ID", key)
    monkeypatch.setenv("APCA_API_SECRET_KEY", secret)
    run_day(tmp_path)
    blob = b"".join(p.read_bytes() for p in (tmp_path / "journal").rglob("*") if p.is_file())
    assert key.encode() not in blob and secret.encode() not in blob


def test_journal_export_command_rebuilds_files(tmp_path):
    t = TradeLog(tmp_path / "jr" / "trade_journal.sqlite", tmp_path / "jr" / "exports")
    t.run_id = "r"
    t.candidate(DAY, {"symbol": "X", "rank": None, "action": "skip", "skip_reason": "adv<1M"})
    cfg = tmp_path / "s.toml"
    cfg.write_text(f'[runtime]\ndata_dir = "{tmp_path}/rt"\n[journal]\ndir = "{tmp_path}/jr"\n')
    assert cli.main(["--config", str(cfg), "journal-export", "--date", DAY.isoformat()]) == 0
    assert (tmp_path / "jr" / "exports" / "daily" / f"summary_{DAY}.md").exists()


def test_overnight_carry_outcome_includes_the_entry_side_modeled_costs(tmp_path):
    """The open_at_close row has no P&L, so the next day's carry row must carry its entry costs;
    otherwise every overnight loss is understated by the whole entry commission and slippage."""
    from types import SimpleNamespace as NS
    t = tl(tmp_path)
    prior = date(2026, 9, 30)
    t.outcome(prior, {"symbol": "CBRS", "status": "open_at_close", "direction": -1, "filled_qty": "45",
                      "entry_price": "177.8300", "planned_stop": "178.92", "modeled_commission": "0.1575",
                      "modeled_slippage": "0.45"})
    day = date(2026, 10, 1)
    fill = {"symbol": "CBRS", "role": "opensweep", "qty": "45", "price": "183.685556", "ts": "2026-10-01T13:33:48Z",
            "commission": "0.1575", "slippage": "0.45"}
    sj = NS(fills=lambda d: [fill], orders=lambda d, s: [],
            trades=lambda d: [NS(symbol="CBRS", state="foreign", direction=0, entry_price=None, atr=None,
                                 stop_price=None)])
    o = build_outcomes(sj, t, day, None, None, None)[0]
    assert o["status"] == "closed" and o["direction"] == -1
    assert o["gross_pnl"] == "-263.50"
    assert o["modeled_commission"] == "0.3150" and o["modeled_slippage"] == "0.90"   # entry + exit side
    assert o["net_pnl_modeled"] == "-264.72"


def test_carry_gross_uses_the_exact_entry_vwap_not_the_rounded_column(tmp_path):
    """entry_price is stored at 4 dp; 0.00004 x 400 sh would shift the carry P&L from the broker's fills."""
    from types import SimpleNamespace as NS
    t = tl(tmp_path)
    t.outcome(date(2026, 9, 30), {"symbol": "ZZ", "status": "open_at_close", "direction": -1, "filled_qty": "400",
                                  "entry_price": "10.0000", "modeled_commission": "0", "modeled_slippage": "0",
                                  "detail": {"entry_price_exact": "10.00004"}})
    fill = {"symbol": "ZZ", "role": "opensweep", "qty": "400", "price": "11", "ts": "2026-10-01T13:33:48Z",
            "commission": "0", "slippage": "0"}
    sj = NS(fills=lambda d: [fill], orders=lambda d, s: [],
            trades=lambda d: [NS(symbol="ZZ", state="foreign", direction=0, entry_price=None, atr=None, stop_price=None)])
    o = build_outcomes(sj, t, date(2026, 10, 1), None, None, None)[0]
    assert o["gross_pnl"] == "-399.98"
