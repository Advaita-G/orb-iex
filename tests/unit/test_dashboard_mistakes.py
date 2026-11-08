"""The loss journal: every closed loss is either reviewed or waiting, and bad lines are reported."""

from __future__ import annotations

import json
from decimal import Decimal

import dashboard_fixture as fx
import pytest

from orb_iex.dashboard.api import Journal
from orb_iex.dashboard.sources import LossJournal


@pytest.fixture()
def root(tmp_path):
    fx.write(tmp_path)
    return tmp_path


def _journal(root):
    return Journal(fx.sources_for(root), fx.settings_for(root))


def _losers(journal):
    return [t for t in journal.all_trades()
            if t.raw["open_qty"] == 0 and t.raw["realized_pnl"] is not None and t.raw["realized_pnl"] < 0]


def test_missing_file_puts_every_closed_loss_in_awaiting_review(root):
    journal = _journal(root)
    book = journal.mistakes()
    assert book["state"] == "missing"
    assert book["reviewed"] == []
    losers = _losers(journal)
    assert losers, "fixture must contain at least one closed loss"
    assert sorted(r["trade_id"] for r in book["awaiting_review"]) == sorted(t.trade_id for t in losers)
    for row in book["awaiting_review"]:
        assert row["suggested"] in ("unlucky", "mistake")
        assert Decimal(row["realized_pnl"]) < 0


def test_reviewed_loss_leaves_the_queue_and_bad_lines_are_reported(root):
    journal = _journal(root)
    loser = _losers(journal)[0]
    path = root / "docs" / "loss-journal.jsonl"
    path.parent.mkdir()
    good = {"day": loser.day, "trade_id": loser.trade_id, "symbol": loser.symbol, "verdict": "mistake",
            "category": "unprotected-entry-fill", "title": "Entry filled with no stop", "cause": "test cause",
            "fix": {"status": "proposed", "branch": "fix/x", "test": "tests/unit/test_x.py"}}
    incident = {"day": loser.day, "trade_id": None, "verdict": "mistake", "category": "false-stop",
                "title": "Paused for no reason",
                "cause": "test incident"}
    path.write_text("\n".join([json.dumps(good), "{not json", json.dumps({"day": "2026-10-26", "verdict": "maybe",
                                                                            "title": "t", "cause": "c"}),
                               json.dumps(incident)]) + "\n")
    book = journal.mistakes()
    assert book["state"] == "ok"
    assert [e["line"] for e in book["errors"]] == [2, 3]
    assert loser.trade_id not in {r["trade_id"] for r in book["awaiting_review"]}
    reviewed = {e["title"]: e for e in book["reviewed"]}
    assert reviewed["Entry filled with no stop"]["trade"]["trade_id"] == loser.trade_id
    assert reviewed["Paused for no reason"]["trade"] is None
    assert reviewed["Paused for no reason"]["fix"] == {"status": "none"}
    assert book["summary"]["mistakes"] == 2
    assert book["summary"]["fix_waiting_approval"] == 1
    assert book["summary"]["no_fix_yet"] == 1


def test_loss_journal_rejects_unknown_fix_status(tmp_path):
    path = tmp_path / "loss-journal.jsonl"
    path.write_text(json.dumps({"day": "2026-10-26", "verdict": "unlucky", "title": "t", "cause": "c",
                                "fix": {"status": "done"}}) + "\n")
    out = LossJournal(path).read()
    assert out["entries"] == []
    assert "fix.status" in out["errors"][0]["error"]


def test_a_later_line_supersedes_the_earlier_one(root):
    journal = _journal(root)
    loser = _losers(journal)[0]
    path = root / "docs" / "loss-journal.jsonl"
    path.parent.mkdir()
    base = {"day": loser.day, "verdict": "mistake", "category": "false-stop", "title": "Paused for no reason",
            "cause": "c"}
    trade = {"day": loser.day, "trade_id": loser.trade_id, "verdict": "unlucky", "title": "Stop-out", "cause": "c"}
    path.write_text("\n".join(json.dumps(r) for r in [
        base, trade, {**base, "fix": {"status": "proposed", "branch": "fix/x", "test": "tests/unit/test_x.py"}},
        {**trade, "verdict": "mistake", "category": "late-stop", "title": "Stop was late"}]) + "\n")
    book = journal.mistakes()
    assert len(book["reviewed"]) == 2
    titles = {e["title"]: e for e in book["reviewed"]}
    assert titles["Paused for no reason"]["fix"]["status"] == "proposed"
    assert "Stop-out" not in titles and titles["Stop was late"]["verdict"] == "mistake"


def test_lessons_count_repeats_and_flag_a_mistake_that_returns_after_its_fix(root):
    journal = _journal(root)
    path = root / "docs" / "loss-journal.jsonl"
    path.parent.mkdir()
    rows = [
        {"day": "2026-10-20", "verdict": "mistake", "category": "late-stop", "title": "Stop late", "cause": "c",
         "fix": {"status": "shipped", "test": "tests/unit/test_x.py"}},
        {"day": "2026-10-22", "verdict": "mistake", "category": "late-stop", "title": "Stop late again", "cause": "c"},
        {"day": "2026-10-22", "verdict": "mistake", "category": "missed-scan", "title": "Missed scan", "cause": "c"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    book = journal.mistakes()
    assert book["errors"] == []
    first = book["lessons"][0]
    assert first["category"] == "late-stop" and first["times"] == 2 and first["recurred_after_fix"]
    assert book["summary"]["repeated"] == 1 and book["summary"]["recurred_after_fix"] == 1


def test_a_mistake_without_category_or_a_fix_without_test_is_rejected(tmp_path):
    path = tmp_path / "loss-journal.jsonl"
    path.write_text(json.dumps({"day": "d", "verdict": "mistake", "title": "t", "cause": "c"}) + "\n"
                    + json.dumps({"day": "d", "verdict": "mistake", "category": "k", "title": "t2", "cause": "c",
                                  "fix": {"status": "shipped"}}) + "\n")
    errors = [e["error"] for e in LossJournal(path).read()["errors"]]
    assert "category" in errors[0] and "fix.test" in errors[1]


def test_unknown_verdict_and_era_are_accepted_and_counted(root):
    """A loss the record cannot judge is reported as unknown, not guessed; old mistakes are counted apart."""
    journal = _journal(root)
    losers = _losers(journal)
    path = root / "docs" / "loss-journal.jsonl"
    path.parent.mkdir()
    old = {"day": losers[0].day, "trade_id": losers[0].trade_id, "verdict": "mistake", "category": "wash-trade",
           "era": "pre-hardening", "title": "old", "cause": "c",
           "fix": {"status": "shipped", "commit": "abc1234", "shipped_on": "2026-10-02", "test": "tests/unit/test_x.py"}}
    unknown = {"day": losers[1].day, "trade_id": losers[1].trade_id, "verdict": "unknown", "title": "t", "cause": "no logs"}
    bad_era = {"day": "2026-10-26", "verdict": "unlucky", "title": "t", "cause": "c", "era": "someday"}
    path.write_text("\n".join(json.dumps(x) for x in (old, unknown, bad_era)) + "\n")
    book = journal.mistakes()
    assert [e["line"] for e in book["errors"]] == [3] and "era" in book["errors"][0]["error"]
    s = book["summary"]
    assert (s["mistakes"], s["pre_hardening"], s["current"], s["unknown"]) == (1, 1, 0, 1)
    assert losers[1].trade_id not in {r["trade_id"] for r in book["awaiting_review"]}


def test_a_fix_that_predates_the_review_is_dated_by_its_commit(root):
    """A pre-hardening mistake fixed on 10-02 must not read as 'came back after a fix' for entries on or before 10-02."""
    path = root / "docs" / "loss-journal.jsonl"
    path.parent.mkdir()
    fix = {"status": "shipped", "commit": "abc1234", "shipped_on": "2026-10-02", "test": "tests/unit/test_x.py"}
    rows = [{"day": d, "trade_id": f"{d}:{s}", "verdict": "mistake", "category": "wash", "title": "t", "cause": "c", "fix": fix}
            for d, s in (("2026-09-30", "A"), ("2026-10-01", "B"), ("2026-10-02", "C"))]
    path.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    lesson = _journal(root).mistakes()["lessons"][0]
    assert lesson["shipped_on"] == "2026-10-02" and lesson["recurred_after_fix"] is False
    rows.append({"day": "2026-10-21", "trade_id": "2026-10-21:D", "verdict": "mistake", "category": "wash", "title": "t", "cause": "c"})
    path.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    assert _journal(root).mistakes()["lessons"][0]["recurred_after_fix"] is True
