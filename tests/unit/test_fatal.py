"""Process fatal-error handling (P0-A item 1): an unexpected exception in the run loop blocks new entries, runs one
broker reconciliation, protects or flattens every ORB-IEX position, persists the failure, and exits non-zero.
Ctrl+C / SIGTERM (KeyboardInterrupt, SystemExit) are NOT swallowed."""

import datetime as dt
import json
import logging
from decimal import Decimal as D
from pathlib import Path

import pytest

from helpers import FakeData, Harness, calendar, et
from orb_iex import cli
from orb_iex.runner import FATAL_FILE, FATAL_META_KEY, FatalRunnerError, Runner
from orb_iex.safety import SafetyError

ROOT = Path(__file__).resolve().parents[2]


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def logs():
    lg = logging.getLogger("orb_iex")
    h = Capture()
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.DEBUG)
    yield h
    lg.removeHandler(h)
    lg.setLevel(old)


def runner_for(h: Harness, mode="paper") -> Runner:
    """A Runner around the harness engine, journal and simulator (the real run() / fatal path)."""
    r = Runner(h.settings, mode, h.clock, client=None, data=FakeData(calendar()), journal=h.j, broker=h.sim,
               events=h.q, calendar=calendar(), account_source=lambda: {"equity": "100000"})
    r.engine = h.engine
    r.schedule = h.schedule
    return r


def boom(*_a, **_k):
    raise RuntimeError("boom: unexpected bug")


def crash(r: Runner) -> FatalRunnerError:
    r.step = boom
    with pytest.raises(FatalRunnerError) as exc:
        r.run()
    return exc.value


def writes(h):
    return list(h.sim.write_log)


def held(h, sym="AAA", qty=100, **kw):
    """A filled entry with its protective stop in place."""
    h.open_session()
    t = h.enter(sym, 1, qty, **kw)
    h.sim.fill(t.entry_order_id, D(qty), D("50.00"))
    h.pump()
    return h.trade(sym)


def persisted(h, r):
    meta = json.loads(h.j.get_meta(FATAL_META_KEY))
    f = json.loads((Path(h.settings.data_dir) / FATAL_FILE).read_text())
    return meta, f


# --- normal loop -------------------------------------------------------------------------------------------------

def test_exception_in_the_normal_loop_blocks_entries_persists_and_exits_non_zero(tmp_path, logs):
    h = Harness(tmp_path)
    h.open_session()
    r = runner_for(h)
    err = crash(r)
    assert isinstance(err.original, RuntimeError) and not err.safety
    assert r.engine.entries_blocked == "fatal_error"
    assert err.report["entries_blocked"] is True and err.report["protect"]["positions"] == {}
    assert writes(h) == []                                    # nothing to protect: no order of any kind
    meta, f = persisted(h, r)
    assert meta["error"] == f["error"] == "RuntimeError: boom: unexpected bug"
    assert set(err.report["persisted"]) >= {"journal", "file"}
    crit = [x for x in logs.records if x.levelno == logging.CRITICAL and getattr(x, "event", "") == "fatal_error"]
    assert crit and crit[0].exc_info and "boom" in "".join(__import__("traceback").format_exception(*crit[0].exc_info))
    # the process would now exit non-zero (7); a new entry from this dying process is refused
    h.j.save_candidates(h.day, [{"symbol": "AAA", "rank": 1, "status": "candidate"}])
    h.engine.place_entries([h.plan("AAA")], {"AAA": {"tradable": True, "shortable": True}},
                           {"buying_power": "1000000"}, h.clock.now())
    assert h.j.trade(h.day.isoformat(), "AAA") is None
    assert [c for c in h.j.candidates(h.day) if c["symbol"] == "AAA"][0]["reason"] == "fatal_error"


def test_keyboard_interrupt_and_system_exit_are_not_swallowed(tmp_path):
    for exc_type in (KeyboardInterrupt, SystemExit):
        h = Harness(tmp_path / exc_type.__name__)
        h.open_session()
        r = runner_for(h)

        def stop(*_a, _t=exc_type, **_k):
            raise _t()
        r.step = stop
        with pytest.raises(exc_type):
            r.run()
        assert r.engine.entries_blocked == ("shutdown" if exc_type is KeyboardInterrupt else None)   # no fatal state
        assert h.j.get_meta(FATAL_META_KEY) is None
        assert not (Path(h.settings.data_dir) / FATAL_FILE).exists()


def test_exit_codes(tmp_path, monkeypatch):
    err = FatalRunnerError(RuntimeError("x"), {})
    monkeypatch.setattr(cli, "_run_mode", lambda args, mode: (_ for _ in ()).throw(err))
    cfg = tmp_path / "s.toml"
    cfg.write_text(f'[runtime]\ndata_dir = "{tmp_path / "rt"}"\nmode = "shadow"\n')
    assert cli.main(["--config", str(cfg), "shadow", "--date", "2026-09-24"]) == cli.EXIT_FATAL == 7
    safety = FatalRunnerError(SafetyError("no"), {})
    monkeypatch.setattr(cli, "_run_mode", lambda args, mode: (_ for _ in ()).throw(safety))
    assert cli.main(["--config", str(cfg), "shadow", "--date", "2026-09-24"]) == cli.EXIT_SAFETY   # not restarted


def test_systemd_restarts_in_5_seconds_and_a_fatal_exit_is_restarted():
    unit = (ROOT / "deploy" / "oracle" / "orb-iex-paper.service").read_text()
    assert "RestartSec=5\n" in unit and "RestartSec=60" not in unit and "Restart=always" in unit
    prevent = [ln for ln in unit.splitlines() if ln.startswith("RestartPreventExitStatus")][0]
    assert "7" not in prevent.split("=", 1)[1].split()          # exit 7 (fatal) must be restarted


# --- open position -----------------------------------------------------------------------------------------------

def test_exception_with_an_open_position_confirms_the_working_stop_and_adds_no_order(tmp_path):
    h = Harness(tmp_path)
    t = held(h)
    before = writes(h)
    err = crash(runner_for(h))
    assert err.report["protect"]["positions"] == {"AAA": {"qty": "100", "action": "protected"}}
    assert err.report["protect"]["reconciled"] is True
    assert writes(h) == before                                # confirmed, not touched: no cancel, no exit
    assert not h.trade("AAA").pending_flatten and h.engine.entries_blocked == "fatal_error"
    assert h.sim.orders[h.engine.current_stop(t).order_id].status == "new"


def test_open_position_whose_stop_does_not_match_is_flattened(tmp_path):
    h = Harness(tmp_path)
    t = held(h)
    stop = h.engine.current_stop(t)
    h.sim.orders[stop.order_id].qty = D(60)                    # Alpaca's stop covers 60 of the 100 shares
    h.engine.reconcile(h.clock.now(), side_effects=False)
    err = crash(runner_for(h))
    assert err.report["protect"]["positions"]["AAA"]["action"] == "flatten_initiated"
    assert h.trade("AAA").pending_flatten
    assert ("cancel", stop.order_id) in writes(h)             # the blocking stop is cancelled first


def test_fatal_report_survives_a_restart_and_is_announced(tmp_path, logs):
    h = Harness(tmp_path)
    held(h)
    crash(runner_for(h))
    logs.records.clear()
    r2 = runner_for(h)                                          # the restarted process: same journal
    r2.step = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        r2.run()
    prev = [x for x in logs.records if getattr(x, "event", "") == "previous_fatal"]
    assert prev and prev[0].levelno == logging.CRITICAL and "boom" in prev[0].getMessage()
    assert r2.engine is h.engine


# --- stop pending ------------------------------------------------------------------------------------------------

def test_exception_while_the_stop_is_pending_flattens_and_converges_to_flat(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    t = h.enter("AAA", 1, 100)
    h.sim.initial_status = "pending_new"                       # the protective stop is accepted but not working yet
    h.sim.fill(t.entry_order_id, D(100), D("50.00"))
    h.pump()
    stop = h.engine.current_stop(h.trade("AAA"))
    assert stop is not None and stop.status == "pending_new"
    err = crash(runner_for(h))
    assert err.report["protect"]["positions"]["AAA"]["action"] == "flatten_initiated"
    assert h.trade("AAA").pending_flatten                      # persisted: the restarted process keeps flattening
    assert ("cancel", stop.order_id) in writes(h)
    assert h.engine.entries_blocked == "fatal_error"
    # the (restarted) engine finishes the job: stop gone -> market exit for the whole position
    h.sim.auto_fill_market, h.sim.last_price["AAA"] = True, D("50.00")
    for _ in range(4):
        h.clock.sleep(3)
        h.engine.tick(h.clock.now())
        h.pump()
    assert h.sim.positions == {}
    exits = [r for r in h.sim.submitted if r.type == "market" and r.symbol == "AAA"]
    assert len(exits) == 1 and exits[0].side == "sell" and exits[0].qty == D(100)


# --- EOD ---------------------------------------------------------------------------------------------------------

def test_exception_in_the_middle_of_eod_still_flattens(tmp_path):
    h = Harness(tmp_path)
    t = held(h)
    h.clock.sleep((et(h.day, 15, 59, 0) - h.clock.now()).total_seconds())     # the end-of-day window
    h.engine._eod_maybe_liquidate = boom                       # EOD cancels the stop, then dies before the exit

    def eod_step():
        h.engine.begin_eod(h.clock.now())
    r = runner_for(h)
    r.step = eod_step
    with pytest.raises(FatalRunnerError) as exc:
        r.run()
    h.pump()
    assert h.sim.orders[h.engine.stop_chain(t)[0].order_id].status == "canceled"     # unprotected at this point...
    assert exc.value.report["protect"]["positions"]["AAA"]["action"] in ('flatten_initiated', 'exit_working')
    exits = [x for x in h.sim.submitted if x.type == "market" and x.symbol == "AAA"]
    assert len(exits) == 1 and exits[0].side == "sell" and exits[0].qty == D(100)    # ...and flattened at once
    assert h.engine.entries_blocked == "fatal_error" and h.trade("AAA").pending_flatten


def test_exception_during_eod_with_the_exit_already_working_adds_nothing(tmp_path):
    h = Harness(tmp_path)
    held(h)
    h.clock.sleep((et(h.day, 15, 59, 0) - h.clock.now()).total_seconds())
    h.engine.begin_eod(h.clock.now())
    h.pump()
    h.engine.tick(h.clock.now())                                # the cancels are confirmed: the EOD exit goes out
    h.pump()
    exits = [x for x in h.sim.submitted if x.type == "market"]
    assert len(exits) == 1
    before = writes(h)
    err = crash(runner_for(h))
    assert err.report["protect"]["positions"]["AAA"]["action"] == "exit_working"
    assert writes(h) == before


def test_unreachable_broker_is_reported_not_retried_forever(tmp_path):
    h = Harness(tmp_path)
    held(h)

    def down(*_a, **_k):
        raise ConnectionError("Alpaca unreachable")
    h.sim.list_orders = down
    err = crash(runner_for(h))
    assert err.report["protect"]["positions"] is None and "reconcile failed" in err.report["protect"]["errors"][0]
    assert h.engine.entries_blocked == "fatal_error"
    assert json.loads(h.j.get_meta(FATAL_META_KEY))["error"].startswith("RuntimeError")
    assert dt.datetime.fromisoformat(err.report["at"])
