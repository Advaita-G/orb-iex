import json
from datetime import date
from decimal import Decimal as D

import test_gapup_shadow as t
from orb_iex import gapup_review as r
from orb_iex.dashboard import gapup as dash
from orb_iex.journal import Journal


def seed(tmp_path):
    root = tmp_path / "runtime"
    gp = root / "gapup_v1"
    gp.mkdir(parents=True)
    ports = {}
    for name in t.g.PORTFOLIOS:
        p, clock = t.make(gp, name)
        ports[name] = (p, clock)
    p, clock = ports["gapup_top10"]
    t.decide(p, [t.row()])
    t.decide(ports["gapup_top5"][0], [t.row()])
    t.run_day(p, clock, [
        (t.et(t.DAY, 10, 0, 0), {"AAA": lambda n: t.q("AAA", 103.9, 104.0, n)}),
        (t.et(t.DAY, 10, 0, 3), {"AAA": lambda n: t.q("AAA", 103.9, 104.0, n)}),
        (t.et(t.DAY, 11, 5, 0), {"AAA": lambda n: t.q("AAA", 107.6, 107.7, n)}),
        (t.et(t.DAY, 11, 5, 3), {"AAA": lambda n: t.q("AAA", 107.6, 107.7, n)})])
    obs = Journal(gp / "observations.sqlite")
    obs.set_meta("provenance", json.dumps({"contract": {"version": t.g.VERSION}, "frozen_at": "2026-11-01T12:00:00+00:00"}))
    obs.save_diagnostic(t.DAY, "gapup_session", {"day": str(t.DAY), "status": "COMPLETE", "data_gaps": []})
    return root


def test_report_has_decisions_costs_and_orb_context(tmp_path):
    root = seed(tmp_path)
    rep = r.build_report(root, t.DAY)
    top10, top5 = rep["portfolios"]["gapup_top10"], rep["portfolios"]["gapup_top5"]
    assert rep["session"]["status"] == "COMPLETE" and rep["version"] == t.g.VERSION
    assert top10["eligible"] == ["AAA"] and top10["day_metrics"]["trades"] == 1
    assert 0 < top10["day_metrics"]["net_2x"] < top10["day_metrics"]["net_1x"]            # doubled costs cost more
    assert top10["closed_trades"][0]["classification"] == "ordinary_strategy_outcome"      # target exit relabeled
    assert top5["day_metrics"]["trades"] == 0 and top5["decided"]
    assert set(rep["orb_same_day"]) == {"baseline_a", "candidate_b"} and rep["automatic_rule_change"] is False
    assert "backtest is history, not validation" in r.render(rep)


def test_missing_session_is_not_collected_not_replayed(tmp_path):
    root = tmp_path / "runtime"
    rep = r.build_report(root, date(2026, 11, 3))
    assert rep["session"]["status"] == "NOT_COLLECTED"
    assert all(p["day_metrics"]["trades"] == 0 for p in rep["portfolios"].values())


def test_metrics_small_sample_flag_and_concentration():
    trips = [{"day": "2026-11-02", "symbol": "A", "exit_time": "x", "net_modeled_pnl": D("100"), "modeled_net_2x": D("90"), "gross_pnl": D("110")},
             {"day": "2026-11-03", "symbol": "B", "exit_time": "y", "net_modeled_pnl": D("-40"), "modeled_net_2x": D("-50"), "gross_pnl": D("-30")}]
    m = dash.lane_metrics(trips)
    assert m["small_sample"] and m["net_1x"] == 60 and m["net_2x"] == 40
    assert m["best_symbol"]["net_without"] == -40 and m["profit_factor"] == 2.5 and m["max_drawdown"] == 40


def test_dashboard_summary_reads_all_lanes(tmp_path):
    root = seed(tmp_path)
    s = dash.summary(root)
    assert set(s["portfolios"]) == {"gapup_top10", "gapup_top5"} and s["qualification"]["qualified"] is False
    assert s["portfolios"]["gapup_top10"]["metrics"]["trades"] == 1
    assert s["same_period_orb"]["baseline_a"]["status"] == "NOT_COLLECTED"


def test_sleep_gap_is_flagged(tmp_path):
    root = seed(tmp_path)
    obs = Journal(root / "gapup_v1/observations.sqlite")
    at = t.et(t.DAY, 9, 37)
    ticks = [at.replace(minute=37 + i // 3, second=(i % 3) * 20) for i in range(9)]            # steady 20 s ticks
    ticks += [t.et(t.DAY, 11, 0), t.et(t.DAY, 11, 0, 20)]                                       # then a long silence
    for n, when in enumerate(ticks, 1):
        obs.save_diagnostic(t.DAY, f"tick:{n}", {"received_at": when.astimezone(t.g.timezone.utc).isoformat()})
    rep = r.build_report(root, t.DAY)
    assert any(g["seconds"] > 3000 for g in rep["collector_gaps"])
    assert "UNRELIABLE DAY" in r.render(rep)
    clean = r.build_report(tmp_path / "other", t.DAY)
    assert clean["collector_gaps"] == []
