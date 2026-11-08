"""Unattended operation: Oracle config, private .env, instance lock, disk guard,
heartbeat, health check, log rotation, daily review and the systemd unit."""

import gzip
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import FakeTransport, basic_plan_routes
from helpers import Harness, et, settings
from orb_iex import cli, http, ops
from orb_iex.config import load_settings
from orb_iex.tradelog import TradeLog

ROOT = Path(__file__).resolve().parents[2]
ORACLE_DATA = "/home/opc/orb_iex_data"


def _keys(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "PKFAKE12345")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "fake-secret-abcdef")
    monkeypatch.delenv("APCA_API_BASE_URL", raising=False)


def _cfg(tmp_path, extra=""):
    cfg = tmp_path / "settings.toml"
    cfg.write_text(f'[runtime]\ndata_dir = "{tmp_path / "rt"}"\n{extra}'
                   f'[safety]\nkill_switch_file = "{tmp_path / "KILL"}"\n'
                   f'[journal]\ndir = "{tmp_path / "rt" / "journal"}"\n')
    return cfg


# --- Oracle VM config and service file ---------------------------------------------

def test_oracle_settings_are_paper_and_persistent():
    s = load_settings(ROOT / "config" / "settings.oracle.toml")
    assert s.mode == "paper" and s.enable_paper_orders
    assert s.data_dir == ORACLE_DATA
    assert s.journal_dir == f"{ORACLE_DATA}/journal"
    assert s.kill_switch_file.startswith(ORACLE_DATA + "/")
    assert s.min_free_disk_mb >= 500 and s.log_retention_days >= 7


def test_service_file_is_paper_restarting_and_has_no_keys():
    unit = (ROOT / "deploy" / "oracle" / "orb-iex-paper.service").read_text()
    assert "APCA" not in unit and "EnvironmentFile" not in unit
    exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert exec_line.endswith(" paper") and "--require-private-env" in exec_line
    assert "settings.oracle.toml" in exec_line and f"{ORACLE_DATA}/.env" in exec_line
    for needed in ("Restart=always", "After=network-online.target", "Wants=network-online.target",
                   "RestartPreventExitStatus=2 3", "WantedBy=multi-user.target", "StartLimitIntervalSec=0"):
        assert needed in unit


def test_deploy_scripts_are_executable_and_order_free():
    for name in ("install.sh", "orbctl"):
        p = ROOT / "deploy" / "oracle" / name
        assert os.access(p, os.X_OK)
        text = p.read_text()
        assert "/v2/orders" not in text
        assert not re.search(r"\bpaper\s*$", text, re.M)          # never starts paper mode itself


# --- private .env --------------------------------------------------------------------

def test_require_private_env_refuses_readable_env(tmp_path, monkeypatch):
    _keys(monkeypatch)
    env = tmp_path / ".env"
    env.write_text("# keys\n")
    env.chmod(0o644)
    code = cli.main(["--config", str(_cfg(tmp_path)), "--env-file", str(env), "--require-private-env",
                     "check-config"])
    assert code == cli.EXIT_CONFIG
    env.chmod(0o600)
    assert cli.main(["--config", str(_cfg(tmp_path)), "--env-file", str(env), "--require-private-env",
                     "check-config"]) == cli.EXIT_OK


def test_require_private_env_refuses_missing_env(tmp_path, monkeypatch):
    _keys(monkeypatch)
    assert cli.main(["--config", str(_cfg(tmp_path)), "--env-file", str(tmp_path / "none.env"),
                     "--require-private-env", "check-config"]) == cli.EXIT_CONFIG


# --- single instance -----------------------------------------------------------------

def test_instance_lock_refuses_second_holder(tmp_path):
    first = ops.InstanceLock(tmp_path).acquire()
    with pytest.raises(ops.InstanceLockError):
        ops.InstanceLock(tmp_path).acquire()
    first.release()
    ops.InstanceLock(tmp_path).acquire().release()        # free again after release


def test_paper_start_refused_while_another_instance_runs(tmp_path, monkeypatch):
    _keys(monkeypatch)
    held = ops.InstanceLock(tmp_path / "rt").acquire()
    try:
        code = cli.main(["--config", str(_cfg(tmp_path)), "--env-file", str(tmp_path / "none.env"), "paper"])
    finally:
        held.release()
    assert code == cli.EXIT_LOCKED


# --- disk guard ----------------------------------------------------------------------

def test_startup_refused_when_disk_nearly_full(tmp_path, monkeypatch):
    _keys(monkeypatch)
    transport = FakeTransport(basic_plan_routes())
    monkeypatch.setattr(http.requests, "Session", lambda: transport)
    monkeypatch.setattr(ops, "free_mb", lambda path: 10)
    code = cli.main(["--config", str(_cfg(tmp_path)), "--env-file", str(tmp_path / "none.env"), "paper"])
    assert code == cli.EXIT_DISK
    assert transport.calls == []                          # stopped before any network call


def test_check_disk():
    assert ops.check_disk("/", 0) >= 0
    with pytest.raises(ops.DiskSpaceError):
        ops.check_disk("/", 10**12)


def test_default_disk_threshold_is_512mb():
    # the 2026-09-28 fix: 1024 MB false-blocked entries on a 10 GB VM boot disk
    assert settings(__import__("pathlib").Path("/tmp")).min_free_disk_mb == 512


def test_free_disk_above_threshold_allows_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "free_mb", lambda path: 600)     # above the 512 MB default
    from test_runner import DAY, make
    r, clock, data, j, sim = make(tmp_path, start=et(DAY, 9, 0))
    r.run(until=et(DAY, 9, 40))
    assert r.engine.entries_blocked is None
    assert [t for t in j.trades(DAY.isoformat()) if t.state != "foreign"]


def test_free_disk_below_threshold_blocks_new_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "free_mb", lambda path: 400)     # below the 512 MB default
    from test_runner import DAY, make
    r, clock, data, j, sim = make(tmp_path, start=et(DAY, 9, 0))
    r.run(until=et(DAY, 9, 40))
    assert r.engine.entries_blocked == "disk_low"
    assert not [t for t in j.trades(DAY.isoformat()) if t.state != "foreign"]


def test_stops_and_exits_still_work_when_disk_below_threshold(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter("OLD")                                        # entry placed while disk was fine
    h.engine.entries_blocked = "disk_low"                      # disk drops below 512 MB mid-session
    h.sim.fill(t.entry_order_id, 100, 50)
    h.pump()
    assert h.engine.current_stop(h.trade("OLD")) is not None   # protective stop still placed
    h.engine.sweep("closesweep", "test exit under disk_low")   # exits still run despite the block
    h.pump()
    assert any(o.role == "closesweep" for o in h.j.orders(h.day.isoformat(), "OLD"))
    h.enter("NEW")
    assert h.trade("NEW") is None                               # new entries still refused


def test_runtime_disk_low_blocks_entries_only(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    h.enter("OLD")
    h.engine.entries_blocked = "disk_low"
    h.enter("NEW")
    assert h.trade("NEW") is None
    assert [c["reason"] for c in h.j.candidates(h.day) if c["symbol"] == "NEW"] == ["disk_low"]
    t = h.trade("OLD")                                    # protective stop still placed after the fill
    h.sim.fill(t.entry_order_id, 100, 50)
    h.pump()
    assert h.engine.current_stop(h.trade("OLD")) is not None


def test_runner_disk_guard_and_heartbeat(tmp_path, monkeypatch):
    from test_runner import DAY, make
    free = {"mb": 10}
    monkeypatch.setattr(ops, "free_mb", lambda path: free["mb"])
    r, clock, data, j, sim = make(tmp_path, start=et(DAY, 9, 0))
    r.run(until=et(DAY, 9, 40))
    assert r.engine.entries_blocked == "disk_low"
    assert not [t for t in j.trades(DAY.isoformat()) if t.state != "foreign"]
    hb = ops.read_heartbeat(r.settings.data_dir)
    assert hb["mode"] == "shadow" and hb["entries_blocked"] == "disk_low" and hb["pid"] == os.getpid()
    assert hb["last_data_update"]
    free["mb"] = 10**6
    r.run(until=et(DAY, 9, 50))
    assert r.engine.entries_blocked is None


# --- health check --------------------------------------------------------------------

class _Client:
    def __init__(self, account_number="PA1234567890"):
        self.account_number = account_number

    def trading_get(self, path, params=None, purpose=""):
        if path == "/v2/account":
            return SimpleNamespace(ok=True, status=200, data={"account_number": self.account_number,
                                                               "status": "ACTIVE"})
        return SimpleNamespace(ok=True, status=200, data=[{"date": "2026-09-28"}])


def test_health_report_all_ok(tmp_path):
    s = settings(tmp_path, runtime__min_free_disk_mb=50)
    now = datetime.now(timezone.utc)
    ops.write_heartbeat(s.data_dir, {"mode": "paper", "phase": "pre", "last_data_update": now.isoformat()})
    rows = {name: (ok, detail) for name, ok, detail in ops.health_report(s, _Client())}
    assert list(rows) == ["bot running", "alpaca paper account", "market calendar", "journal writable",
                          "last successful data update", "disk space"]
    assert all(ok for ok, _ in rows.values()), rows


def test_health_report_flags_stale_heartbeat_and_live_account(tmp_path):
    s = settings(tmp_path)
    ops.write_heartbeat(s.data_dir, {"mode": "paper"})
    later = datetime.now(timezone.utc) + timedelta(minutes=10)
    rows = {name: ok for name, ok, _ in ops.health_report(s, _Client("123456789"), now=later)}
    assert rows["bot running"] is False
    assert rows["alpaca paper account"] is False
    assert rows["last successful data update"] is False


def test_health_command_is_read_only(tmp_path, monkeypatch, capsys):
    _keys(monkeypatch)
    transport = FakeTransport(basic_plan_routes())
    monkeypatch.setattr(http.requests, "Session", lambda: transport)
    code = cli.main(["--config", str(_cfg(tmp_path, "min_free_disk_mb = 50\n")),
                     "--env-file", str(tmp_path / "none.env"), "health"])
    out = capsys.readouterr().out
    assert "OK    alpaca paper account" in out and "OK    market calendar" in out
    assert "FAIL  bot running" in out and code == cli.EXIT_FAILED    # nothing running here
    assert {c["method"] for c in transport.calls} == {"GET"}


# --- log rotation --------------------------------------------------------------------

def test_log_rotation_compresses_and_deletes(tmp_path):
    today = date(2026, 9, 26)
    for d in (today, today - timedelta(days=1), today - timedelta(days=40)):
        (tmp_path / f"orb_iex-{d:%Y%m%d}.jsonl").write_text('{"msg": "x"}\n')
    (tmp_path / f"orb_iex-{today - timedelta(days=45):%Y%m%d}.jsonl.gz").write_bytes(b"")
    out = ops.rotate_logs(tmp_path, 30, today)
    assert out == {"compressed": 1, "deleted": 2}
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["orb_iex-20260925.jsonl.gz", "orb_iex-20260926.jsonl"]
    assert gzip.decompress((tmp_path / "orb_iex-20260925.jsonl.gz").read_bytes()) == b'{"msg": "x"}\n'


# --- daily review --------------------------------------------------------------------

def _outcome(sym, direction, net, r, exit_reason="stop"):
    return {"symbol": sym, "direction": direction, "status": "closed", "gross_pnl": str(net + 1),
            "net_pnl_modeled": str(net), "net_pnl_paper_costs": str(net), "realized_r": str(r),
            "exit_reason": exit_reason, "exit_time": "2026-09-24T15:00:00+00:00", "mfe_r": "1.5",
            "mae_r": "0.5", "modeled_commission": "0.5", "modeled_slippage": "0.5", "actual_slippage": "0.25"}


def test_review_command_prints_metrics(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    s = load_settings(cfg)
    t = TradeLog(Path(s.journal_dir) / "trade_journal.sqlite", Path(s.journal_dir) / "exports")
    t.run_id = "r1"
    day = date(2026, 9, 24)
    t.outcome(day, _outcome("A", 1, 30, 3, "eod"))
    t.outcome(day, _outcome("B", -1, -10, -1))
    t.candidate(day, {"symbol": "A", "rank": 1, "action": "trade"})
    assert cli.main(["--config", str(cfg), "review", "--mode", "paper"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    for line in ("trades:            2", "wins / losses:     1 / 1", "net modeled P&L:   20.00",
                 "profit factor:     3.00", "average R:         1.00", "stop-out rate:     50.0%",
                 "average MFE:       1.50R", "average MAE:       0.50R", "slippage cost:     actual 0.50",
                 "long P&L:          30.00", "short P&L:         -10.00"):
        assert line in out, line


def test_review_without_journal(tmp_path, capsys):
    assert cli.main(["--config", str(_cfg(tmp_path)), "review"]) == cli.EXIT_OK
    assert "no trading journal" in capsys.readouterr().out
