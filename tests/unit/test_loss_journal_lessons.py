"""The bot learns from docs/loss-journal.jsonl: every fixed mistake stays enforced by a test.

A mistake whose fix is proposed, approved or shipped names its regression test. If that test file
(or the named test function) disappears, this suite fails, so a lesson cannot be lost silently.
"""
from __future__ import annotations

import re
from pathlib import Path

from orb_iex.dashboard.sources import LossJournal

PROJECT = Path(__file__).resolve().parents[2]
JOURNAL = PROJECT / "docs" / "loss-journal.jsonl"


def test_loss_journal_is_valid():
    book = LossJournal(JOURNAL).read()
    assert book["state"] == "ok", f"{JOURNAL} is missing"
    assert book["errors"] == [], book["errors"]


def test_every_fixed_mistake_keeps_its_regression_test():
    missing = []
    for e in LossJournal(JOURNAL).read()["entries"]:
        fix = e.get("fix") or {}
        if e["verdict"] != "mistake" or fix.get("status") not in ("proposed", "approved", "shipped"):
            continue
        path, _, name = fix["test"].partition("::")
        target = PROJECT / path
        if not target.is_file():
            missing.append(f"{e['day']} {e['title']}: {path} does not exist")
        elif name and not re.search(rf"^def {re.escape(name)}\(", target.read_text(), re.M):
            missing.append(f"{e['day']} {e['title']}: {name} not found in {path}")
    assert not missing, "lessons lost:\n" + "\n".join(missing)
