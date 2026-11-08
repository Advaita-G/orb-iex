"""The improvement-pipeline view: ledger backlog and experiments, plus the newest daily report."""
from __future__ import annotations

import json

import dashboard_fixture as fx
import pytest

from orb_iex.dashboard.api import Journal


@pytest.fixture()
def root(tmp_path):
    fx.write(tmp_path)
    return tmp_path


def _journal(root):
    return Journal(fx.sources_for(root), fx.settings_for(root))


def test_missing_ledger_and_report_are_reported_not_fatal(root):
    d = _journal(root).improvement()
    assert d["state"] == "missing" and d["backlog"] == [] and d["latest_report"] is None


def test_ledger_and_latest_report_are_served(root):
    h = {"kind": "hypothesis", "id": "H1", "day": "2026-10-27", "title": "t", "rationale": "r",
         "status": "proposed", "priority": 1}
    e = {"kind": "experiment", "id": "X1", "hypothesis": "H1", "day": "2026-10-28", "spec": {}, "data_window": {},
         "verdict": "rejected"}
    journal = _journal(root)
    docs = journal.sources.data_dir.parent / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "experiment-ledger.jsonl").write_text(json.dumps(h) + "\n" + json.dumps(e) + "\n")
    reports = journal.sources.data_dir / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "daily-review-2026-10-27.md").write_text("old")
    (reports / "daily-review-2026-10-28.md").write_text("new")
    d = journal.improvement()
    assert [b["id"] for b in d["backlog"]] == ["H1"] and d["experiments"][0]["id"] == "X1"
    assert d["latest_report"] == {"day": "2026-10-28", "text": "new"}
    assert "improvement" in journal.snapshot()
