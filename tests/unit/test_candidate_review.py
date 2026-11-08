from datetime import date
import hashlib

from orb_iex.candidate_review import review
from orb_iex.journal import Journal


def test_report_reads_paired_pre_outcome_decisions_without_writing(tmp_path):
    path = tmp_path / "journal.sqlite"
    day = date(2026, 10, 28)
    j = Journal(path)
    j.save_diagnostic(day, "ab_plans", {"AAA": {"baseline_qty": 100, "candidate_qty": 50}})
    j.save_diagnostic(day, "ab_quote:AAA", {"decision_time": "2026-10-28T13:35:03Z", "ab": {
        "baseline_reason": None, "candidate_reason": "candidate_displayed_size"}})
    j.save_candidates(day, [{"symbol": "AAA", "status": "submitted"}])
    j.db.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    out = review(path, day)
    assert out["status"] == "COLLECTING"
    assert out["candidate_reasons"] == {"candidate_displayed_size": 1}
    assert out["all_signal_decisions"][0]["status"] == "submitted"
    assert "excluded winners" in out["hypotheses"][0]["next_check"]
    assert out["outcome_evidence"] == {}
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert review(path, date(2026, 10, 29))["status"] == "INCOMPLETE"


def test_missing_journal_does_not_create_a_database(tmp_path):
    import sqlite3
    import pytest
    path = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        review(path, date(2026, 10, 28))
    assert not path.exists()


def test_report_accounts_only_for_closed_fills_and_keeps_cause_unknown(tmp_path):
    path = tmp_path / "journal.sqlite"
    day = date(2026, 10, 28)
    j = Journal(path)
    for symbol, side, role, price in [("AAA", "buy", "entry", "10"), ("AAA", "sell", "stop1", "9.9"),
                                      ("BBB", "buy", "entry", "10")]:
        j.db.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (symbol + role, "100", day.isoformat(), symbol, role, side, "100", price,
                      "2026-10-28T13:36:00Z", "0.35", "1"))
    j.db.close()
    out = review(path, day)["outcome_evidence"]
    assert out["AAA"]["gross_closed_cash"] == "-10.0"
    assert out["AAA"]["net_closed_cash_1x"] == "-12.70"
    assert out["AAA"]["net_closed_cash_2x"] == "-15.40"
    assert out["AAA"]["failed_breakout_observed"]
    assert "unresolved" in out["AAA"]["classification"]
    assert out["BBB"]["gross_closed_cash"] is None
