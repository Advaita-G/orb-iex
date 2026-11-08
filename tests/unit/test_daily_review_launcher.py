"""Behaviour of deploy/macos/daily-review.sh with a fake `claude` (no model, no network)."""
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "macos" / "daily-review.sh"
SRC = Path(__file__).resolve().parents[2] / "src"

FAKE = """#!/bin/bash
[ "$1" = "--version" ] && exit 0
echo "$@" >>"$FAKE_CALLS"
day=$(echo "$@" | grep -o 'DAY=[0-9-]*' | head -1 | cut -d= -f2)
n=$(grep -c "DAY=$day" "$FAKE_CALLS")
[ "${FAKE_FAIL_FIRST:-0}" = 1 ] && [ "$n" -lt 2 ] && exit 3
[ "${FAKE_ALWAYS_FAIL:-0}" = 1 ] && exit 3
echo "# report" >"$FAKE_REPORTS/daily-review-$day.md"
"""


def launch(tmp_path, now, seed=True, **env):
    root = tmp_path / "root"
    (root / "runtime" / "reports").mkdir(parents=True, exist_ok=True)
    if seed:                      # 2026-10-26 already reviewed, so only the day under test is due
        (root / "runtime/reports/daily-review-2026-10-26.md").write_text("done")
    fake = tmp_path / "claude"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    wrapper = tmp_path / "py"
    wrapper.write_text(f"#!/bin/bash\nPYTHONPATH={SRC} exec {sys.executable} \"$@\"\n")
    wrapper.chmod(0o755)
    calls = tmp_path / "calls"
    calls.touch()
    e = {**os.environ, "ORB_DAILY_ROOT": str(root), "ORB_DAILY_PY": str(wrapper), "ORB_DAILY_CLAUDE": str(fake),
         "ORB_DAILY_NOW": now, "ORB_DAILY_RETRY_WAIT_S": "0", "FAKE_CALLS": str(calls),
         "FAKE_REPORTS": str(root / "runtime" / "reports"), **env}
    r = subprocess.run(["bash", str(SCRIPT)], env=e, capture_output=True, text=True, timeout=60)
    return r, root, calls.read_text().splitlines()


def test_missed_days_are_reviewed_oldest_first_and_rerun_is_a_noop(tmp_path):
    r, root, calls = launch(tmp_path, "2026-10-27T21:00:00+00:00", seed=False)
    assert r.returncode == 0 and ["DAY=2026-10-26" in c for c in calls] == [True, False]
    assert len(calls) == 2 and "DAY=2026-10-27" in calls[1]
    assert (root / "runtime/reports/daily-review-2026-10-27.md").exists()
    assert not (root / "runtime/daily-review.lock").exists()
    (tmp_path / "calls").unlink()
    r2, _, calls2 = launch(tmp_path, "2026-10-27T21:00:00+00:00", seed=False)
    assert r2.returncode == 0 and calls2 == []


def test_failure_is_retried_once_then_succeeds(tmp_path):
    r, root, calls = launch(tmp_path, "2026-10-27T21:00:00+00:00", FAKE_FAIL_FIRST="1")
    assert r.returncode == 0 and len(calls) == 2
    assert not (root / "runtime/reports/daily-review-2026-10-27.attempts").exists()


def test_persistent_failure_exits_nonzero_and_counts_attempts(tmp_path):
    r, root, calls = launch(tmp_path, "2026-10-27T21:00:00+00:00", FAKE_ALWAYS_FAIL="1")
    assert r.returncode == 1 and len(calls) == 2
    assert (root / "runtime/reports/daily-review-2026-10-27.attempts").read_text().strip() == "2"
    assert not (root / "runtime/reports/daily-review-2026-10-27.md").exists()


def test_holiday_and_weekend_are_never_reviewed(tmp_path):
    # Thanksgiving, then a Saturday: the earlier real sessions are caught up, the closed days are not.
    for now, closed in (("2026-11-26T22:00:00+00:00", "2026-11-26"), ("2026-10-31T22:00:00+00:00", "2026-10-31")):
        r, _, calls = launch(tmp_path, now)
        assert r.returncode == 0 and calls and not any(f"DAY={closed}" in c for c in calls)
        (tmp_path / "calls").unlink()


def test_live_lock_blocks_and_stale_lock_is_replaced(tmp_path):
    root = tmp_path / "root"
    lock = root / "runtime" / "daily-review.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))
    r, _, calls = launch(tmp_path, "2026-10-27T21:00:00+00:00")
    assert r.returncode == 0 and calls == [] and lock.exists()
    (lock / "pid").write_text("999999")
    r, _, calls = launch(tmp_path, "2026-10-27T21:00:00+00:00")
    assert r.returncode == 0 and len(calls) == 1 and not lock.exists()
