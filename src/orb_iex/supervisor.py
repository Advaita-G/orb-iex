"""Relaunch loop for unattended PAPER operation. It never trades; it only restarts `paper-auto`.

paper-auto handles exactly one calendar session per launch and fails closed on any doubt, so the
supervisor's job is small: relaunch after a clean session, back off after a failure, never run two
copies (the child's flock does that), keep the Mac awake, and stop on an operator request.
Every relaunch revalidates ownership, broker state and the breaker policy from scratch.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .logging_setup import LOGGER_NAME, log_event

log = logging.getLogger(LOGGER_NAME)

EXIT_OK, EXIT_LOCKED = 0, 6
MAX_DELAY_SECONDS = 900
STOP_FILE = "SUPERVISOR_STOP"


def next_delay(code: int, failures: int) -> tuple[float, int]:
    """(seconds to wait before the next launch, consecutive failure count)."""
    if code == EXIT_OK:
        return 5.0, 0                                    # session finished: next launch waits for the next open
    if code == EXIT_LOCKED:
        return 60.0, failures                            # a live instance owns the account: do not fight it
    failures += 1
    return float(min(30 * 2 ** (failures - 1), MAX_DELAY_SECONDS)), failures


def child_command(config: str, env_file: str, caffeinate: bool | None = None) -> list[str]:
    cmd = [sys.executable, "-m", "orb_iex", "--config", config, "--env-file", env_file, "paper-auto"]
    use = (sys.platform == "darwin" and shutil.which("caffeinate") is not None) if caffeinate is None else caffeinate
    return ["caffeinate", "-is", *cmd] if use else cmd


def supervise(spawn: Callable[[], int], sleep: Callable[[float], None], data_dir: str | Path,
              max_launches: int | None = None) -> int:
    stop = Path(data_dir) / STOP_FILE
    failures = launches = 0
    while max_launches is None or launches < max_launches:
        if stop.exists():
            log_event(log, logging.WARNING, "supervisor", f"{stop} present: not launching; remove it to resume")
            return 0
        launches += 1
        code = spawn()
        delay, failures = next_delay(code, failures)
        level = logging.INFO if code == EXIT_OK else logging.ERROR if failures < 5 else logging.CRITICAL
        log_event(log, level, "supervisor", f"paper-auto exited {code}; consecutive failures {failures}; "
                  f"next launch in {delay:.0f}s", exit_code=code, failures=failures, delay_s=delay)
        try:
            sleep(delay)
        except KeyboardInterrupt:
            return 0
    return 0


def run(config: str, env_file: str, data_dir: str | Path) -> int:
    cmd = child_command(config, env_file)
    try:
        return supervise(lambda: subprocess.call(cmd), __import__("time").sleep, data_dir)
    except KeyboardInterrupt:
        return 0
