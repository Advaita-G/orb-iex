import os
import signal
import subprocess
import sys
import time

import pytest

from orb_iex.ops import InstanceLock, InstanceLockError, heartbeat_state, pid_alive
from orb_iex.supervisor import MAX_DELAY_SECONDS, STOP_FILE, child_command, next_delay, supervise
from helpers import et
from test_paper_auto import DAY


def test_backoff_resets_after_a_clean_session_and_is_capped():
    assert next_delay(0, 7) == (5.0, 0)
    assert next_delay(6, 3) == (60.0, 3)
    delays, f = [], 0
    for _ in range(8):
        d, f = next_delay(7, f); delays.append(d)
    assert delays[:3] == [30, 60, 120] and delays[-1] == MAX_DELAY_SECONDS


def test_supervisor_relaunches_after_failures_and_obeys_stop_file(tmp_path):
    codes = iter([7, 7, 0, 4]); slept = []
    n = supervise(lambda: next(codes), slept.append, tmp_path, max_launches=4)
    assert n == 0 and slept == [30, 60, 5, 30]
    (tmp_path / STOP_FILE).write_text('x')
    assert supervise(lambda: pytest.fail('must not launch'), slept.append, tmp_path) == 0


def test_child_command_keeps_the_mac_awake_when_asked():
    assert child_command('c.toml', '.env', caffeinate=True)[:2] == ['caffeinate', '-is']
    assert 'caffeinate' not in child_command('c.toml', '.env', caffeinate=False)


def test_killed_process_leaves_no_false_lock_but_a_live_one_still_blocks(tmp_path):
    code = ("import sys;from orb_iex.ops import InstanceLock;l=InstanceLock(sys.argv[1]).acquire();"
            "print('held',flush=True);import time;time.sleep(60)")
    p = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == 'held'
        with pytest.raises(InstanceLockError):
            InstanceLock(tmp_path).acquire()                    # live second process is refused
        p.send_signal(signal.SIGKILL); p.wait()
        assert (tmp_path / 'orb_iex.lock').read_text().strip() == str(p.pid)   # stale pid text remains...
        lock = InstanceLock(tmp_path).acquire()                 # ...but the OS freed the flock: no false lock
        assert (tmp_path / 'orb_iex.lock').read_text().strip() == str(os.getpid())
        lock.release()
    finally:
        if p.poll() is None:
            p.kill()


def test_heartbeat_state_distinguishes_healthy_degraded_stale_dead_missing():
    from datetime import timedelta
    now = et(DAY, 10, 0); ts = lambda s: (now - timedelta(seconds=s)).isoformat()
    base = {'pid': os.getpid(), 'updated_at': ts(10)}
    assert heartbeat_state(None, now)[0] == 'missing'
    assert heartbeat_state(base, now)[0] == 'healthy'
    assert heartbeat_state({**base, 'health': 'degraded', 'health_reasons': ['breaker:auto_clock_skew']}, now)[0] == 'degraded'
    assert heartbeat_state({**base, 'updated_at': ts(900)}, now)[0] == 'stale'
    assert heartbeat_state({**base, 'pid': 2 ** 22 + 12345}, now)[0] == 'dead'
    assert not pid_alive(None)
