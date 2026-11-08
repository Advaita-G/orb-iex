"""Unattended operation helpers: disk guard, single-instance lock, heartbeat,
log retention, private .env check, health check and daily review.

None of this touches strategy rules or places orders. The health check only
uses GET requests.
"""

from __future__ import annotations

import errno
import gzip
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HEARTBEAT_FILE = "health/heartbeat.json"
LOCK_FILE = "orb_iex.lock"
HEARTBEAT_MAX_AGE_SECONDS = 180


class DiskSpaceError(RuntimeError):
    """Persistent storage is nearly full; the bot must not start new work."""


class InstanceLockError(RuntimeError):
    """Another ORB-IEX process already holds the lock for this data directory."""


# -- disk ------------------------------------------------------------------------

def free_mb(path: str | Path) -> int:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free // (1024 * 1024)


def check_disk(path: str | Path, min_free_mb: int) -> int:
    free = free_mb(path)
    if free < min_free_mb:
        raise DiskSpaceError(f"only {free} MB free on the disk holding {path} (minimum {min_free_mb} MB); "
                             "free space before starting")
    return free


# -- single instance -----------------------------------------------------------------

class InstanceLock:
    """Exclusive, non-blocking flock on <data_dir>/orb_iex.lock (released on exit)."""

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / LOCK_FILE
        self.fh = None
        self.recovered_dead_pid: int | None = None   # a previous holder that died without releasing

    def _recorded_pid(self, fh) -> int | None:
        try:
            fh.seek(0)
            raw = fh.read().strip()
            return int(raw) if raw.isdigit() else None
        except (OSError, ValueError):
            return None

    @classmethod
    def for_account(cls, fingerprint: str) -> "InstanceLock":
        """Same OS user/account cannot evade exclusion by changing data_dir.

        Cross-machine exclusion still requires distinct account ownership;
        this lock coordinates processes on this host only.
        """
        if len(fingerprint) != 16 or any(c not in '0123456789abcdef' for c in fingerprint):
            raise ValueError('invalid account fingerprint')
        return cls(Path.home() / '.cache' / 'orb_iex' / 'accounts' / fingerprint)

    def acquire(self) -> "InstanceLock":
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = self._recorded_pid(fh)
            fh.close()
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                who = f" (pid {holder})" if holder else ""
                raise InstanceLockError(f"another ORB-IEX process{who} is already running on {self.path.parent}")
            raise
        # The kernel lock is the only truth: a recorded pid whose process is gone is just a leftover file.
        prev = self._recorded_pid(fh)
        if prev and prev != os.getpid() and not pid_alive(prev):
            self.recovered_dead_pid = prev
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self.fh = fh
        return self

    def release(self) -> None:
        if self.fh:
            self.fh.close()
            self.fh = None


# -- keep the host awake -----------------------------------------------------------------

class KeepAwake:
    """macOS suspends a laptop (and every process on it) when idle; a suspended bot sleeps through the
    scan window. `caffeinate -i -s -w <pid>` blocks idle/system sleep for as long as this process lives.
    Closing the lid on battery still sleeps the Mac: that cannot be prevented from software."""

    def __init__(self, platform: str | None = None, which=shutil.which, popen=None):
        self.platform = sys.platform if platform is None else platform
        self.which, self.popen = which, popen or subprocess.Popen
        self.proc = None
        self.state = "not_needed" if self.platform != "darwin" else "unstarted"

    def start(self) -> str:
        """'active' | 'unavailable' | 'not_needed'."""
        if self.platform != "darwin":
            return self.state
        exe = self.which("caffeinate")
        if exe is None:
            self.state = "unavailable"
            return self.state
        try:
            self.proc = self.popen([exe, "-i", "-s", "-w", str(os.getpid())],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
            self.state = "active"
        except OSError:
            self.state = "unavailable"
        return self.state

    def stop(self) -> None:
        if self.proc is not None:
            try:
                self.proc.terminate()
            except OSError:
                pass
            self.proc = None


# -- private .env -----------------------------------------------------------------

def check_private_env(path: str | Path) -> None:
    """Unattended mode: the .env file must exist and be readable by its owner only (600)."""
    p = Path(path)
    if not p.is_file():
        raise PermissionError(f"{p} not found; create it with your PAPER keys and run: chmod 600 {p}")
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(f"{p} has permissions {oct(mode)}; run: chmod 600 {p}")


# -- heartbeat -----------------------------------------------------------------------

def write_heartbeat(data_dir: str | Path, body: dict) -> None:
    if body.get('mode') in ('paper', 'gapup_shadow') and body.get('session'):
        from .integrity import runtime_integrity
        integrity = runtime_integrity(data_dir, body['mode'], body['session'], body.get('journal_path'))
        reasons = list(dict.fromkeys([*(body.get('health_reasons') or []), *integrity['issues']]))
        health = body.get('health', 'healthy')
        body = {**body, 'integrity': integrity, 'health_reasons': reasons,
                'health': 'degraded' if reasons and health == 'healthy' else health}
    path = Path(data_dir) / HEARTBEAT_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({**body, "pid": os.getpid(),
                                   "updated_at": datetime.now(timezone.utc).isoformat()}, default=str))
        os.replace(tmp, path)
    except OSError as exc:
        logging.getLogger(__name__).error('heartbeat_write_failed: %s', exc)


def read_heartbeat(data_dir: str | Path) -> dict | None:
    try:
        return json.loads((Path(data_dir) / HEARTBEAT_FILE).read_text())
    except (OSError, ValueError):
        return None


def heartbeat_state(hb: dict | None, now: datetime) -> tuple[str, str]:
    """(state, detail): healthy | degraded | stale (alive but silent) | dead (PID gone) | missing."""
    if not hb:
        return "missing", "no heartbeat file yet"
    try:
        age = (now - datetime.fromisoformat(hb["updated_at"])).total_seconds()
    except (KeyError, ValueError):
        return "stale", "unreadable heartbeat timestamp"
    if not pid_alive(hb.get("pid")):
        return "dead", f"pid {hb.get('pid')} is not running (last heartbeat {int(age)}s ago)"
    if age > HEARTBEAT_MAX_AGE_SECONDS:
        return "stale", f"pid {hb.get('pid')} alive but silent for {int(age)}s"
    state = hb.get("health", "healthy")
    reasons = ",".join(hb.get("health_reasons") or [])
    return state, f"pid {hb.get('pid')}, heartbeat {int(age)}s ago" + (f", {reasons}" if reasons else "")


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


# -- log retention ---------------------------------------------------------------------

def rotate_logs(log_dir: str | Path, retention_days: int, today: date | None = None) -> dict:
    """gzip daily JSON-lines logs older than today; delete logs older than retention_days."""
    today = today or date.today()
    d = Path(log_dir)
    out = {"compressed": 0, "deleted": 0}
    if not d.is_dir():
        return out
    for p in sorted(d.glob("orb_iex-*.jsonl*")):
        stem = p.name.split(".")[0]                     # orb_iex-YYYYMMDD
        try:
            day = datetime.strptime(stem[len("orb_iex-"):], "%Y%m%d").date()
        except ValueError:
            continue
        age = (today - day).days
        if age > retention_days:
            p.unlink(missing_ok=True)
            out["deleted"] += 1
        elif age >= 1 and p.suffix == ".jsonl":
            gz = p.with_name(p.name + ".gz")
            with p.open("rb") as src, gzip.open(gz, "wb") as dst:
                shutil.copyfileobj(src, dst)
            p.unlink()
            out["compressed"] += 1
    return out


# -- health check -------------------------------------------------------------------------

def health_report(settings, client=None, now: datetime | None = None) -> list[tuple[str, bool, str]]:
    """(check, ok, detail) rows. Read-only: GET requests and a temp-file write test."""
    now = now or datetime.now(timezone.utc)
    rows: list[tuple[str, bool, str]] = []
    hb = read_heartbeat(settings.data_dir)
    state, detail = heartbeat_state(hb, now)
    rows.append(("bot running", state == "healthy",
                 f"{state}: {detail}" + (f", mode {hb.get('mode')}, phase {hb.get('phase')}" if hb else "")))
    if client is not None:
        try:
            r = client.trading_get("/v2/account", purpose="health:account")
            ok = r.ok and isinstance(r.data, dict) and str(r.data.get("account_number", "")).startswith(
                settings.expected_account_prefix) and r.data.get("status") == "ACTIVE"
            rows.append(("alpaca paper account", ok, f"HTTP {r.status}" + (
                f", ...{str(r.data.get('account_number'))[-4:]} {r.data.get('status')}" if isinstance(r.data, dict)
                else "")))
        except Exception as exc:
            rows.append(("alpaca paper account", False, f"{type(exc).__name__}: {exc}"))
        try:
            today = now.date()
            r = client.trading_get("/v2/calendar", params={"start": today.isoformat(),
                                                           "end": (today + timedelta(days=7)).isoformat()},
                                   purpose="health:calendar")
            ok = r.ok and isinstance(r.data, list) and bool(r.data)
            nxt = r.data[0]["date"] if ok else "-"
            rows.append(("market calendar", ok, f"HTTP {r.status}, next session {nxt}"))
        except Exception as exc:
            rows.append(("market calendar", False, f"{type(exc).__name__}: {exc}"))
    jdir = Path(settings.journal_dir)
    try:
        jdir.mkdir(parents=True, exist_ok=True)
        probe = jdir / f".write-test-{os.getpid()}"
        probe.write_text("ok")
        probe.unlink()
        rows.append(("journal writable", True, str(jdir)))
    except OSError as exc:
        rows.append(("journal writable", False, f"{jdir}: {exc}"))
    last = (hb or {}).get("last_data_update")
    if last:
        age_h = (now - datetime.fromisoformat(last)).total_seconds() / 3600
        rows.append(("last successful data update", age_h <= 96, f"{last} ({age_h:.1f} h ago)"))
    else:
        rows.append(("last successful data update", False, "none recorded yet"))
    try:
        free = free_mb(settings.data_dir)
        rows.append(("disk space", free >= settings.min_free_disk_mb,
                     f"{free} MB free (minimum {settings.min_free_disk_mb} MB)"))
    except OSError as exc:
        rows.append(("disk space", False, str(exc)))
    return rows


# -- daily review -------------------------------------------------------------------------

def review_lines(summary: dict, day: str) -> list[str]:
    def v(x, suffix=""):
        return "-" if x in (None, "") else f"{x}{suffix}"
    trades = summary["trades"]
    stop_rate = f"{summary['stop_outs'] / trades * 100:.1f}%" if trades else "-"
    return [
        f"ORB-IEX daily review {day} (record only; no rule changes from this)",
        f"trades:            {trades}",
        f"wins / losses:     {summary['wins']} / {summary['losses']}",
        f"net modeled P&L:   {v(summary['net_pnl_modeled'])}",
        f"profit factor:     {v(summary['profit_factor'])}",
        f"average R:         {v(summary['average_r'])}",
        f"stop-out rate:     {stop_rate}",
        f"average MFE:       {v(summary['average_mfe_r'], 'R')}",
        f"average MAE:       {v(summary['average_mae_r'], 'R')}",
        f"slippage cost:     actual {v(summary['total_actual_slippage'])}, "
        f"modeled {v(summary['total_modeled_slippage'])}",
        f"long P&L:          {summary['long']['net_pnl']} ({summary['long']['trades']} trades)",
        f"short P&L:         {summary['short']['net_pnl']} ({summary['short']['trades']} trades)",
    ]
