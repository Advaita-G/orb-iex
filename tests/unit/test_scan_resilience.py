"""Oct 8 failure class: a transient disconnect or a host suspend must not silently cost the day's scan,
and a real safety latch must still block it."""
import logging
import subprocess
import sys

import pytest

from helpers import et
from orb_iex import ops
from orb_iex.http import ReadOnlyClient
from orb_iex.secrets import Credentials
from test_paper_auto import DAY, make, owner  # noqa: F401  (owner is an autouse fixture)


def kinds(h):
    return {b[0] for b in h.j.breakers()}


def steps(r, h, n, gap=5):
    for _ in range(n):
        h.clock.advance(gap)
        r.step()
        if r.schedule:
            h.pump()


def test_host_suspend_across_the_scan_window_is_reported_as_a_suspend_not_as_not_running(tmp_path, caplog):
    r, h, c = make(tmp_path, start=et(DAY, 9, 20))
    r.data.assets = lambda day: []
    mono = [0.0]
    r.mono = lambda: mono[0]
    for _ in range(3):
        h.clock.advance(5); mono[0] += 5; r.step()
    h.clock.advance(40 * 60)                      # the Mac slept 09:20 -> 10:00: wall time moved, monotonic did not
    mono[0] += 1
    with caplog.at_level(logging.WARNING, logger='orb_iex'):
        r.step()
    assert h.j.day(DAY)['scan_status'] == 'missed'
    assert r.suspensions and r.suspensions[0][1] >= et(DAY, 10, 0)
    text = ' '.join(rec.getMessage() for rec in caplog.records)
    assert 'suspended' in text and 'not running' not in text


def test_transient_disconnect_just_before_the_scan_does_not_suppress_it(tmp_path):
    r, h, c = make(tmp_path, start=et(DAY, 9, 34, 50))
    r.data.assets = lambda day: []
    c.down = True
    assert r.step() == 2.0                        # short retry near the scan, not a 60 s sleep through the window
    assert 'auto_control_failure' in kinds(h) and not r.entry_open
    c.down = False
    steps(r, h, 3)                                # three fresh healthy checks >= 10 s apart in total
    steps(r, h, 3)
    assert h.j.day(DAY)['scan_status'] == 'ok'    # scan ran inside its grace window, after fresh validation
    assert 'auto_control_failure' not in kinds(h) and r.entry_open


def test_stale_transient_breaker_blocks_the_scan_until_fresh_checks_and_then_clears(tmp_path):
    r, h, c = make(tmp_path, start=et(DAY, 9, 34, 0))
    r.data.assets = lambda day: []
    h.j.set_breaker('auto_clock_skew', detail='ClockSkewError: left over from a dead process')
    steps(r, h, 2)
    assert h.j.day(DAY)['scan_status'] is None and not r.entry_open     # no scan on stale evidence
    steps(r, h, 30)
    assert h.j.day(DAY)['scan_status'] == 'ok' and 'auto_clock_skew' not in kinds(h)


def test_manual_breaker_still_blocks_the_scan_permanently(tmp_path):
    r, h, c = make(tmp_path, start=et(DAY, 9, 34, 0))
    r.data.assets = lambda day: []
    h.j.set_breaker('overnight_position', 'ZZZ', 'unflattened broker quantity -5')
    steps(r, h, 40)
    assert h.j.day(DAY)['scan_status'] == 'blocked' and not r.entry_open


def test_flapping_connection_through_the_window_never_opens_the_gate(tmp_path):
    r, h, c = make(tmp_path, start=et(DAY, 9, 34, 50))
    r.data.assets = lambda day: []
    for i in range(30):
        c.down = i % 2 == 0
        steps(r, h, 1, gap=2)
    c.down = False
    assert not r.entry_open and h.sim.submitted == []


# -- host sleep prevention --------------------------------------------------------------------

class FakeProc:
    terminated = False

    def terminate(self):
        self.terminated = True


def test_keep_awake_runs_caffeinate_bound_to_this_pid_on_macos():
    seen = {}
    proc = FakeProc()

    def popen(cmd, **kw):
        seen['cmd'] = cmd
        return proc
    k = ops.KeepAwake(platform='darwin', which=lambda n: '/usr/bin/caffeinate', popen=popen)
    assert k.start() == 'active'
    assert seen['cmd'][:3] == ['/usr/bin/caffeinate', '-i', '-s'] and seen['cmd'][-2:] == ['-w', str(ops.os.getpid())]
    k.stop()
    assert proc.terminated


def test_keep_awake_reports_missing_tool_and_is_inert_elsewhere():
    assert ops.KeepAwake(platform='darwin', which=lambda n: None).start() == 'unavailable'
    assert ops.KeepAwake(platform='linux').start() == 'not_needed'


# -- stale vs live lock ---------------------------------------------------------------------------

def test_lock_left_by_a_dead_process_is_recovered_and_reported(tmp_path):
    dead = subprocess.Popen([sys.executable, '-c', 'pass']); dead.wait()
    (tmp_path / ops.LOCK_FILE).write_text(str(dead.pid))
    lock = ops.InstanceLock(tmp_path).acquire()
    assert lock.recovered_dead_pid == dead.pid
    assert (tmp_path / ops.LOCK_FILE).read_text() == str(ops.os.getpid())
    lock.release()


def test_live_holder_still_blocks_a_second_instance_and_is_named(tmp_path):
    first = ops.InstanceLock(tmp_path).acquire()
    try:
        with pytest.raises(ops.InstanceLockError, match=str(ops.os.getpid())):
            ops.InstanceLock(tmp_path).acquire()
        assert (tmp_path / ops.LOCK_FILE).read_text() == str(ops.os.getpid())   # the loser did not clobber it
    finally:
        first.release()


# -- stale pooled connections ---------------------------------------------------------------------

def test_connection_reset_drops_the_pool_before_retrying():
    import requests

    class Transport:
        def __init__(self):
            self.calls = 0
            self.closed = 0

        def request(self, *a, **k):
            self.calls += 1
            if self.calls == 1:
                raise requests.ConnectionError('reset by peer')

            class R:
                status_code = 200
                headers = {}
                text = '[]'

                def json(self):
                    return []
            return R()

        def close(self):
            self.closed += 1
    t = Transport()
    client = ReadOnlyClient(Credentials('K' * 20, 'S' * 20), 100, 100, transport=t, sleep=lambda s: None)
    assert client.trading_get('/v2/orders').ok
    assert t.closed == 1 and t.calls == 2
