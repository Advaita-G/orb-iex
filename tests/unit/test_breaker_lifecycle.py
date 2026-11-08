"""Breaker lifecycle: self-healing only on fresh evidence; manual breakers never clear on their own."""
import json
from datetime import timedelta

import pytest

from helpers import et
from orb_iex.breakers import Evidence, POLICIES, policy, is_manual
from orb_iex.http import ApiResponse
from test_paper_auto import DAY, at, make, owner, ready  # noqa: F401  (owner is an autouse fixture)


def kinds(h):
    return {b[0] for b in h.j.breakers()}


def run_steps(r, h, n, gap=5):
    for _ in range(n):
        h.clock.advance(gap)
        r.step()
        h.pump()


class SkewedClock:
    """Wrap Controls so its /v2/clock reply is off by `skew` seconds."""
    def __init__(self, c, h):
        self.c, self.h, self.skew = c, h, 0.0
        self.orig = c.trading_get
        c.trading_get = self.get

    def get(self, path, **kw):
        resp = self.orig(path, **kw)
        if path == '/v2/clock' and self.skew:
            data = dict(resp.data)
            data['timestamp'] = (self.h.clock.now() + timedelta(seconds=self.skew)).isoformat()
            return ApiResponse(200, data, {}, 'clock')
        return resp


def test_policy_table_lists_only_checkable_conditions():
    assert policy('auto_clock_skew').kind == 'transient' and policy('auto_data_failure').kind == 'session'
    assert policy('auto_safety_failure').kind == 'transient'
    for manual in ('auto_external_kill', 'auto_account_unverified', 'auto_foreign_inventory', 'overnight_position',
                   'unprotected_position', 'sign_flip', 'duplicate_order', 'stuck_pending_order', 'shutdown_incomplete'):
        assert is_manual(manual) and manual not in POLICIES


def test_evidence_needs_consecutive_spaced_healthy_checks_and_resets_on_failure():
    e = Evidence(); pol = policy('auto_clock_skew'); t = et(DAY, 9, 0)
    e.observe(t, True); e.observe(t + timedelta(seconds=1), True)  # too close: counted once
    assert len(e.samples) == 1 and not e.satisfied(pol)
    e.observe(t + timedelta(seconds=6), True); e.observe(t + timedelta(seconds=12), True)
    assert e.satisfied(pol)
    e.observe(t + timedelta(seconds=18), False)
    assert not e.satisfied(pol) and e.samples == []


def test_clock_skew_breaker_blocks_then_self_resolves_after_fresh_healthy_checks(tmp_path):
    r, h, c = ready(tmp_path); skew = SkewedClock(c, h)
    skew.skew = -2.79; run_steps(r, h, 1)
    assert 'auto_clock_skew' in kinds(h) and not r.entry_open and r.failed
    skew.skew = 0.0
    run_steps(r, h, 1)
    assert 'auto_clock_skew' in kinds(h) and not r.entry_open       # one good check is not evidence
    run_steps(r, h, 4)
    assert 'auto_clock_skew' not in kinds(h) and not r.failed
    res = r.report['breaker_resolutions']
    assert res and res[0]['kind'] == 'auto_clock_skew' and res[0]['evidence']['healthy_checks'] >= 3
    assert r.entry_open                                              # gate reopens through the normal checks


def test_safety_summary_breaker_resolves_with_its_transient_cause(tmp_path):
    r, h, _ = ready(tmp_path)
    r._fail('auto_safety_failure', 'unsafe breaker or unresolved intent during trading')
    h.j.set_breaker('auto_clock_skew', detail='temporary broker clock skew')

    run_steps(r, h, 3)

    assert not kinds(h) and not r.failed and r.entry_open


def test_safety_summary_breaker_waits_for_unresolved_intent(tmp_path):
    r, h, _ = ready(tmp_path)
    r._fail('auto_safety_failure', 'unsafe breaker or unresolved intent during trading')
    h.j.save_intent(dict(intent_id='unknown', ts=h.clock.now().isoformat(), symbol='AAA', role='entry',
                        client_order_id='orbiex-20261005-AAA-entry', mutation='submit', state='AMBIGUOUS', attempt=1))

    run_steps(r, h, 4)

    assert 'auto_safety_failure' in kinds(h) and r.failed and not r.entry_open


def test_flapping_clock_never_resolves(tmp_path):
    r, h, c = ready(tmp_path); skew = SkewedClock(c, h)
    skew.skew = -3.0; run_steps(r, h, 1)
    for _ in range(4):
        skew.skew = 0.0; run_steps(r, h, 1)
        skew.skew = 3.0; run_steps(r, h, 1)
    assert 'auto_clock_skew' in kinds(h) and not r.entry_open


def test_slow_reply_is_not_mistaken_for_skew(tmp_path):
    r, h, c = ready(tmp_path); orig = c.trading_get

    def slow(path, **kw):
        stamp = h.clock.now()
        if path == '/v2/clock':
            resp = orig(path, **kw); h.clock.advance(3.0)  # 3 s of network latency: midpoint is 1.5 s off, within 2 s
            return ApiResponse(200, dict(resp.data, timestamp=(stamp + timedelta(seconds=1.5)).isoformat()), {}, 'clock')
        return orig(path, **kw)
    c.trading_get = slow
    run_steps(r, h, 2)
    assert 'auto_clock_skew' not in kinds(h)


def test_broker_outage_resolves_without_a_restart_and_entries_reopen(tmp_path):
    r, h, c = ready(tmp_path)
    c.down = True; run_steps(r, h, 1)
    assert 'auto_control_failure' in kinds(h) and not r.entry_open
    c.down = False; run_steps(r, h, 6)
    assert 'auto_control_failure' not in kinds(h) and r.entry_open


def test_resolution_survives_restart_and_uses_fresh_evidence_in_the_new_process(tmp_path):
    r, h, c = ready(tmp_path); c.down = True; run_steps(r, h, 1)
    assert 'auto_control_failure' in kinds(h)
    c.down = False
    r2, h2, _ = make(tmp_path, start=h.clock.now(), sim=h.sim, journal=h.j)
    assert 'auto_control_failure' in kinds(h2)                          # persisted, not silently dropped
    run_steps(r2, h2, 1)
    assert 'auto_control_failure' in kinds(h2)                          # new process has zero evidence yet
    run_steps(r2, h2, 6)
    assert 'auto_control_failure' not in kinds(h2)


def test_manual_breaker_is_never_cleared_and_blocks_transient_resolution(tmp_path):
    r, h, c = ready(tmp_path); c.down = True; run_steps(r, h, 1)
    h.j.set_breaker('overnight_position', 'ZZZ', 'unflattened broker quantity -5')
    c.down = False; run_steps(r, h, 8)
    assert {'overnight_position', 'auto_control_failure'} <= kinds(h) and not r.entry_open


def test_account_identity_change_is_permanent(tmp_path):
    r, h, c = ready(tmp_path)
    c.account = dict(c.account, id='someone-else', account_number='PAOTHER')
    run_steps(r, h, 1)
    assert 'auto_account_unverified' in kinds(h)
    c.account = {'id': 'auto-fixture', 'account_number': 'PAAUTO', 'status': 'ACTIVE', 'equity': '100000'}
    run_steps(r, h, 8)
    assert 'auto_account_unverified' in kinds(h) and not r.entry_open


def test_session_scoped_data_failure_clears_only_in_a_later_session_when_flat(tmp_path):
    r, h, c = ready(tmp_path)
    h.j.set_breaker('auto_data_failure', detail='corporate-action metadata unavailable or incomplete')
    h.j.db.execute("UPDATE breakers SET ts=? WHERE kind='auto_data_failure'", (et(DAY, 9, 36).isoformat(),))  # journal stamps wall time; pin it to the fake session
    r._resolve_breakers(h.clock.now(), r.schedule.session)
    assert 'auto_data_failure' in kinds(h)                               # same session: no
    next_day = type(r.schedule.session)(**{**r.schedule.session.__dict__, 'day': DAY + timedelta(days=1)}) \
        if hasattr(r.schedule.session, '__dict__') else None
    assert next_day is not None
    r._resolve_breakers(h.clock.now(), next_day)
    assert 'auto_data_failure' not in kinds(h)


def test_transient_breaker_does_not_turn_a_clean_flat_shutdown_into_a_fatal_exit(tmp_path):
    r, h, c = ready(tmp_path); c.down = True; run_steps(r, h, 1); c.down = False
    assert 'auto_control_failure' in kinds(h)
    report = r._graceful_shutdown('sigterm')                              # does not raise
    assert report['status'] == 'SAFE' and report['exposure_flat'] is True


def test_phase_history_is_not_noisy_while_waiting_for_the_scan(tmp_path):
    r, h, c = make(tmp_path, start=et(DAY, 9, 31))
    r.data.assets = lambda day: []
    run_steps(r, h, 12)
    phases = [t['phase'] for t in r.report['transitions']]
    assert phases.count('RECOVERING') <= 1 and phases.count('READY FOR SCAN') <= 1 and len(phases) < 8


def test_heartbeat_reports_degraded_while_stream_is_down_or_a_breaker_is_latched_and_healthy_after(tmp_path):
    from orb_iex.ops import read_heartbeat
    r, h, c = ready(tmp_path)
    r.stream = object()                                   # stand-in: only presence matters to the health summary
    run_steps(r, h, 8, gap=31)
    assert read_heartbeat(h.settings.data_dir)['health'] == 'healthy'
    h.q.put(('status', 'disconnected')); run_steps(r, h, 2, gap=31)  # drained on one step, reported on the next
    hb = read_heartbeat(h.settings.data_dir)
    assert hb['health'] == 'degraded' and 'order_stream_down' in hb['health_reasons']
    h.q.put(('status', 'connected')); run_steps(r, h, 2, gap=31)
    assert read_heartbeat(h.settings.data_dir)['health'] == 'healthy'
    c.down = True; run_steps(r, h, 2, gap=31)
    assert any(x.startswith('breaker:') for x in read_heartbeat(h.settings.data_dir)['health_reasons'])
