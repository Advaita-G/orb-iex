"""One PAPER session, gated recovery, and a durable report using the existing engine.

External KILL is only read. Its latch recovers after removal and safety checks. Ownership,
broker clock and calendar must agree before any regular-session order write.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import timedelta
from pathlib import Path

from .calendar import NY
from .orders import TERMINAL, parse_client_order_id, role_kind
from .preflight import verify_environment
from .runner import Runner, FatalRunnerError, log
from .breakers import Evidence, policy as breaker_policy
from .safety import BrokerUnavailableError, ClockSkewError, SafetyError, kill_switch_active
from .logging_setup import log_event


def write_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(report, f, default=str, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class AutoPaperRunner(Runner):
    def __init__(self, *args, verified_account, **kwargs):
        super().__init__(*args, **kwargs)
        if self.mode != 'paper':
            raise SafetyError('paper-auto requires PAPER mode')
        self.entry_open = False
        self.recovery_ready = False
        self.kill_latched = False
        self.failed = False
        self.controls_at = None
        self.controls_valid = False
        self.regular_open = False
        self.target = None
        self.open_snapshot = None
        self.last_report = None
        self.evidence = Evidence()
        self.report_path = Path(self.settings.data_dir) / 'health/paper-auto.json'
        self.report = {'command': 'paper-auto', 'startup_at': self.clock.now().isoformat(),
                       'account_fingerprint_suffix': verified_account['fingerprint'][-4:],
                       'owner_instance': verified_account['instance_id'], 'status': 'WAITING',
                       'entry_gate_opened_at': None, 'transitions': [], 'breaker_history': [],
                       'breaker_resolutions': []}
        self.engine.entry_guard = self._entry_reason
        self.engine.regular_market_guard = self._regular_market
        self.engine.regular_market_state = self._regular_market_state
        self.before_recovery = self._prepare_recovery
        self.before_entries = self._authorize_entries
        self.broker.mutation_guard = self._mutation_guard
        self._phase('START')

    def _regular_market(self, now):
        return self._regular_market_state(now) == 'open'

    def _regular_market_state(self, now):
        """'open' or 'closed' from a fresh (<= 10 s) valid broker clock check; otherwise 'unknown'."""
        if not (self.controls_valid and self.controls_at is not None
                and 0 <= (now - self.controls_at).total_seconds() <= 10):
            return 'unknown'
        if self.schedule and any(self.controls_at < boundary <= now
                                 for boundary in (self.schedule.session.open, self.schedule.session.close)):
            return 'unknown'
        return 'open' if self.regular_open else 'closed'

    def _controls(self, force=False):
        now = self.clock.now()
        # While latched or unverified, re-check quickly so recovery evidence accrues; idle off-hours is slow.
        interval = 5 if (self.regular_open or self.failed or not self.controls_valid) and self.controls_at else 60
        crossed_boundary = self.controls_at and self.schedule and any(
            self.controls_at < boundary <= now for boundary in (self.schedule.session.open, self.schedule.session.close))
        if not force and not crossed_boundary and self.controls_at and (now - self.controls_at).total_seconds() < interval:
            return self.controls_valid
        self.controls_valid = False
        self.regular_open = False
        try:
            summary = verify_environment(self.client, self.settings, 'paper', now=self.clock.now)
            if summary['fingerprint'][-4:] != self.report['account_fingerprint_suffix']:
                raise SafetyError('account identity changed during paper-auto')
            before = self.clock.now()
            resp = self.client.trading_get('/v2/clock', purpose='paper_auto:clock')
            after = self.clock.now()
            if not resp.ok or not isinstance(resp.data, dict) or type(resp.data.get('is_open')) is not bool:
                raise ConnectionError('broker market clock unavailable or ambiguous')
            from datetime import datetime
            server = datetime.fromisoformat(resp.data['timestamp'])
            # Midpoint, not the reply time: a slow request must not look like a skewed clock.
            if server.tzinfo is None or abs((before + (after - before) / 2 - server).total_seconds()) > 2:
                raise ClockSkewError('broker market clock is stale or local clock skewed')
            self.regular_open = resp.data['is_open']
            self.controls_valid = True
            self.report['broker_clock'] = {k: resp.data.get(k) for k in ('timestamp','is_open','next_open','next_close')}
        except ClockSkewError as exc:
            self._fail('auto_clock_skew', f'{type(exc).__name__}: {exc}')
        except (BrokerUnavailableError, ConnectionError, KeyError, TypeError, ValueError) as exc:
            self._fail('auto_control_failure', f'{type(exc).__name__}: {exc}')
        except SafetyError as exc:  # identity, ownership, allowlist, rejected keys: never self-heal
            self._fail('auto_account_unverified', f'{type(exc).__name__}: {exc}')
        self.evidence.observe(self.clock.now(), self.controls_valid)
        # A reply crossing the bell must not make its pre-bell sample look current.
        self.controls_at = min(before, server) if self.controls_valid else self.clock.now()
        return self.controls_valid

    def _phase(self, phase):
        if not self.report['transitions'] or self.report['transitions'][-1]['phase'] != phase:
            self.report['transitions'].append({'at': self.clock.now().isoformat(), 'phase': phase})
            log_event(log, logging.INFO, 'paper_auto_phase', phase)

    def _fail(self, kind, reason):
        reason = re.sub(r'\b[0-9a-f]{16}\b', lambda m: '…' + m[0][-4:], reason)
        self.entry_open = False
        self.failed = True
        self.evidence.reset()  # a breaker is only ever cleared by checks taken after its latest failure
        if not any(b[0] == kind and b[2] == reason for b in self.j.breakers()):
            self.j.set_breaker(kind, detail=reason)
        self.report['status'] = 'FAILED CLOSED'
        self.report['failure_reason'] = reason
        log_event(log, logging.ERROR, kind, reason)

    def _exposure_clear(self):
        return (self.engine._broker_snapshot_valid and not self.j.intents(True) and not self.engine.gate.journal_failed
                and not self.j.owned_qty() and not self.engine.positions()
                and all(o.status in TERMINAL for o in self.j.orders()))

    def _resolve_breakers(self, now, session):
        """Clear self-healing breakers on fresh evidence only (see breakers.py for the policy)."""
        pending = [(b, breaker_policy(b[0])) for b in self.j.breakers() if breaker_policy(b[0])]
        if not pending or not self.controls_valid:
            return
        if any(not breaker_policy(b[0]) and b[0] != 'auto_external_kill' for b in self.j.breakers()):
            return  # a manual breaker present: leave every latch in place for the operator
        from datetime import datetime
        for b, pol in pending:
            # An operator pause must not retain repaired infrastructure failures or clear safety state.
            if self.kill_latched and (pol.kind != 'transient' or b[0] == 'auto_safety_failure'):
                continue
            if pol.kind == 'transient':
                ok = self.evidence.satisfied(pol) and self.engine._broker_snapshot_valid \
                    and not self.j.intents(True) and not self.engine.gate.journal_failed
                proof = self.evidence.summary()
            else:  # session-scoped: an earlier session's failure, account flat, a new scan re-tests the input
                try:
                    failed_day = datetime.fromisoformat(b[3]).astimezone(NY).date()
                except (TypeError, ValueError):
                    failed_day = session.day
                ok = failed_day < session.day and self._exposure_clear()
                proof = {'failed_session': str(failed_day), 'current_session': str(session.day), 'flat': ok}
            if not ok:
                continue
            self.j.clear_breaker(b[0], b[1])
            item = {'kind': b[0], 'symbol': b[1], 'detail': b[2], 'resolved_at': now.isoformat(), 'evidence': proof}
            self.report['breaker_resolutions'].append(item)
            log_event(log, logging.WARNING, 'breaker_resolved', f'{b[0]} resolved on fresh evidence', **item)
            if self.tlog is not None:
                self.tlog.event(session.day, 'breaker_resolved', None, dedupe_key=f"breaker_resolved:{b[0]}:{now.isoformat()}",
                                detail=item, event_time=now.isoformat())
        if not any(breaker_policy(b[0]) or b[0].startswith('auto_') for b in self.j.breakers()):
            if self.failed and self.report['status'] == 'FAILED CLOSED':
                self.report['status'] = 'RECOVERING'
                self.report.pop('failure_reason', None)
            self.failed = False

    def _kill(self):
        if kill_switch_active(self.settings.kill_switch_file) and not self.kill_latched:
            self.kill_latched = True
            self._fail('auto_external_kill', 'external KILL observed; new entries latched off until safe recovery')
        return self.kill_latched

    def _recover_kill(self):
        # Removing KILL permits recovery in the running process only after the
        # same flat-account and journal checks used by the startup gate.
        if (not self.kill_latched or kill_switch_active(self.settings.kill_switch_file)
                or not self.controls_valid or not self._exposure_clear()
                or self.open_snapshot or self.engine.entries_blocked
                or any(b[0] != 'auto_external_kill' for b in self.j.breakers())):
            return
        if self.j.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise SafetyError('journal integrity check failed')
        self.j.clear_breaker('auto_external_kill')
        self.kill_latched = self.failed = False
        self.report.pop('failure_reason', None)
        self.report['status'] = 'RECOVERING'
        self._phase('RECOVERING')

    def _entry_reason(self, active_coid=None):
        self._kill()
        if not self.entry_open or self.failed or self.kill_latched:
            return 'paper_auto_gate_closed'
        if not self._controls() or not self._regular_market(self.clock.now()):
            return 'paper_auto_market_unverified'
        # The HTTP hook runs after write-ahead journaling. Permit only its own
        # currently executing entry intent; old/other ambiguous intents still block.
        unresolved = [i for i in self.j.intents(True) if not (
            active_coid and i['client_order_id'] == active_coid and i['state'] == 'SENDING'
            and i['role'] == 'entry' and i['mutation'] == 'submit'
            and i['symbol'] in self.engine.gate.busy)]
        if self.j.breakers() or unresolved or self.engine.gate.journal_failed:
            return 'paper_auto_safety_state'
        return None

    def _mutation_guard(self, method, body):
        # Called at Alpaca's shared HTTP choke point, including retries and fatal cleanup.
        if not self._controls() or not self.controls_valid:
            raise SafetyError('paper-auto ownership/connectivity unverified; write refused')
        if method.lower() != 'delete' and not self._regular_market(self.clock.now()):
            raise SafetyError('paper-auto regular market unverified; write refused')
        parsed = parse_client_order_id((body or {}).get('client_order_id', ''))
        if (method.lower() == 'post' and parsed and parsed[2] == 'entry'
                and self._entry_reason((body or {})['client_order_id'])):
            raise SafetyError('paper-auto internal entry gate closed')

    def _prepare_recovery(self, now):
        self.entry_open = False
        self.recovery_ready = False
        self.engine.reconcile(now, side_effects=False)
        if self.j.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise SafetyError('journal integrity check failed')
        # Startup recovery cancels existing bot entries and flattens proven owned
        # inventory through reconcile_symbol. It never invents a new exit path.
        for symbol in set(self.j.owned_qty()) | {o.symbol for o in self.j.orders() if o.status not in TERMINAL}:
            self.j.save_risk(symbol, exit_role='flatten', reason='paper-auto startup recovery')
            t = self.j.trade(self.engine.dkey, symbol)
            if t:
                t.pending_flatten = True
                self.j.save_trade(t)

    def _open_gate(self):
        e = self.engine
        live = [o for o in self.j.orders() if o.status not in TERMINAL]
        if (self.failed or self.kill_latched or not self._regular_market(self.clock.now())
                or not e._broker_snapshot_valid or e.positions() or self.j.owned_qty()
                or live or self.j.intents(True) or e.gate.journal_failed or e.entries_blocked):
            return
        # Startup and runtime recovery both require revalidated safety gates
        # before recognizing an operator-removed KILL.
        breakers = self.j.breakers()
        if any(b[0] != 'auto_external_kill' for b in breakers):
            return
        if self.j.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise SafetyError('journal integrity check failed')
        if breakers:
            self.j.clear_breaker('auto_external_kill')
        if not self.recovery_ready:
            self._phase('READY FOR SCAN')
        self.recovery_ready = True

    def _authorize_entries(self):
        self._kill()
        if not self._controls(force=True) or not self.recovery_ready or self.failed or self.kill_latched:
            return False
        self.engine.reconcile(self.clock.now(), side_effects=False)
        self._check_foreign()
        self._open_gate()
        snapshot = self.j.diagnostic(self.schedule.session.day, 'corporate_action_snapshot') or {}
        if snapshot.get('complete') is not True or snapshot.get('error'):
            self._fail('auto_data_failure', 'corporate-action metadata unavailable or incomplete')
            return False
        if (self.failed or not self._regular_market(self.clock.now()) or self.j.breakers()
                or self.j.intents(True) or self.j.owned_qty() or self.engine.positions()
                or any(o.status not in TERMINAL for o in self.j.orders())):
            return False
        self.report['entry_gate_opened_at'] = self.clock.now().isoformat()
        self._phase('TRADING')
        self._persist(force=True)  # durable decision before entries can reach the broker
        self.entry_open = True
        return True

    def _check_foreign(self):
        foreign = self.engine.unrelated_positions()
        orders = self.broker.list_orders('open')
        self.open_snapshot = orders
        foreign_orders = [o for o in orders if parse_client_order_id(o.client_order_id) is None]
        if foreign or foreign_orders:
            self._fail('auto_foreign_inventory', 'manual/foreign inventory present; preserved, entries blocked')

    def _session_for(self, now):
        if self.target is not None:
            return self._calendar(now).session(self.target)
        return super()._session_for(now)

    def step(self):
        try:
            return self._auto_step()
        except ConnectionError as exc:
            self.controls_valid = self.regular_open = False
            self.evidence.reset()
            self._fail('auto_broker_failure', str(exc))
            self._persist(readable=False, force=True)
            return self._failure_wait()
        except Exception:
            self.entry_open = False
            raise

    def _failure_wait(self):
        """Back off 60 s after a failed control/broker check, except around the scan: the opening scan has a
        short grace window, so a transient blip just before it must not put the loop to sleep through it."""
        now = self.clock.now()
        session = self.schedule.session if self.schedule else (
            self.calendar.today(now) if self.calendar is not None else None)
        if session is not None:
            st = self.settings
            scan = session.open + timedelta(minutes=5, seconds=st.scan_delay_seconds)
            if scan - timedelta(seconds=90) <= now < scan + timedelta(seconds=st.scan_grace_seconds):
                return 2.0
        return 60.0

    def _auto_step(self):
        now = self.clock.now()
        self._ops_tick(now)  # heartbeat must keep beating off-hours and while failed closed
        self._kill()
        if not self._controls():
            self._phase('FAILED CLOSED')
            self._persist(readable=False)
            return self._failure_wait()
        now = self.clock.now()
        session = self._session_for(now)
        if session is None:
            self._fail('auto_calendar_failure', 'no broker calendar session available')
            self._persist(readable=False)
            return 60.0
        if self.schedule is None or self.schedule.session.day != session.day:
            self._start_session(session, now)
        if self._regular_market_state(now) == 'unknown':
            self.entry_open = False
            return 1.0
        self._drain_events(now)
        self.engine.reconcile(now, side_effects=False)
        self._check_foreign()
        self._resolve_breakers(now, session)
        self._recover_kill()
        if self.kill_latched:
            for o in self.j.orders():
                if role_kind(o.role) == 'entry' and o.status not in TERMINAL:
                    self.engine._cancel(o, 'external KILL: cancel owned entry')
        in_session = session.open <= now < session.close
        if self.regular_open and not in_session:
            self.evidence.reset()
            self._fail('auto_calendar_failure', 'broker clock and calendar disagree')
        if not self.regular_open or not in_session:
            self.entry_open = False
            if in_session:
                self.evidence.reset()
                self._fail('auto_market_closed', 'scheduled session unexpectedly closed at broker')
            self._phase('WAITING' if not in_session else 'FAILED CLOSED')
            self._persist()
            # Prepare once off-market; a slow prep cannot lead to an entry before recovery.
            if now < session.open:
                self._ensure_prep(session, now)
            return min(60.0, max(1.0, (session.open-now).total_seconds())) if now < session.open else 60.0
        self.engine.tick(now)
        if not self.entry_open:
            if not self.recovery_ready:
                self._phase('RECOVERING')
            self._open_gate()
            if self.recovery_ready and self.j.day(session.day)['scan_status'] == 'ok':
                self._authorize_entries()
        elif self.j.breakers() or self.j.intents(True):
            self._fail('auto_safety_failure', 'unsafe breaker or unresolved intent during trading')
        wait = super().step()
        self._persist()
        return wait

    def scan(self, session, now):
        if not self.recovery_ready or self.failed or self._kill():
            if self._gate_may_recover():
                # Only self-healing conditions stand in the way (a transient broker/clock breaker awaiting fresh
                # evidence, or recovery still reconciling): keep retrying until the scan deadline instead of
                # throwing the day away. Nothing is scanned or submitted while the gate is closed.
                self.scan_defer_reason = ('entry gate closed: ' + (self.report.get('failure_reason') or 'recovery pending'))
                return
            self.j.mark_scanned(session.day, 'blocked')
            log_event(log, logging.WARNING, 'paper_auto_scan_blocked', 'internal entry gate closed at scan')
            return
        self.scan_defer_reason = None
        super().scan(session, now)
        if self.j.day(session.day)['scan_status'] != 'ok':
            self._fail('auto_data_failure', 'required scan data unavailable before deadline')
        elif not (self.j.diagnostic(session.day, 'corporate_action_snapshot') or {}).get('complete'):
            self._fail('auto_data_failure', 'corporate-action metadata unavailable or incomplete')
        elif self.j.diagnostic(session.day, 'history_exclusions'):
            # Candidate exclusions remain symbol-scoped. The existing feature gate
            # cannot submit an excluded symbol; clean candidates use normal rules.
            self.report['data_exclusions'] = self.j.diagnostic(session.day, 'history_exclusions')

    def _gate_may_recover(self):
        """True when the closed gate can still open on its own before the scan deadline: no KILL and every
        latched breaker is a transient one that resolves on fresh evidence."""
        return not self.kill_latched and all(
            (pol := breaker_policy(b[0])) is not None and pol.kind == 'transient' for b in self.j.breakers())

    def _persist(self, readable=True, final=False, force=False):
        now = self.clock.now()
        if not final and not force and self.last_report and (now-self.last_report).total_seconds() < 5:
            return
        self.report['updated_at'] = now.isoformat()
        self.report['entry_gate_open'] = self.entry_open
        self.report['external_kill_latched'] = self.kill_latched
        breakers = self.j.breakers()
        for b in breakers:
            item = {'kind': b[0], 'symbol': b[1], 'detail': b[2]}
            if item not in self.report['breaker_history']:
                self.report['breaker_history'].append(item)
        self.report['breakers'] = [dict(zip(('kind','symbol','detail','at'), b)) for b in breakers]
        self.report['unresolved_intents'] = [{k:i[k] for k in ('intent_id','symbol','role','state')}
                                             for i in self.j.intents(True)]
        orders = self.j.orders()
        self.report['recovery_actions'] = [{'symbol':o.symbol,'coid':o.client_order_id,
            'side':o.side,'qty':str(o.qty),'filled_qty':str(o.filled_qty),'status':o.status}
            for o in orders if role_kind(o.role) == 'exit']
        day = self.schedule.session.day if self.schedule else None
        current = [o for o in orders if day and o.day == day.isoformat()]
        self.report['session'] = day.isoformat() if day else None
        self.report['counts'] = {'candidates':len(self.j.candidates(day)) if day else 0,
            'entries':sum(o.role=='entry' for o in current),
            'fills':len(self.j.fills(day.isoformat())) if day else 0,
            'stops':sum(role_kind(o.role)=='stop' for o in current),
            'exits':sum(role_kind(o.role)=='exit' for o in current)}
        self.report['cutoff'] = ({'due': now >= self.schedule.entry_cutoff,
            'live_entries': [o.client_order_id for o in orders if o.role == 'entry' and o.status not in TERMINAL]}
            if day else None)
        self.report['eod'] = self.j.day(day)['eod_status'] if day else None
        self.report['final_positions'] = ({s:str(q) for s,q in self.engine.positions().items()}
                                          if readable and self.engine._broker_snapshot_valid else None)
        self.report['final_open_orders'] = ([{'coid':o.client_order_id,'symbol':o.symbol,
            'side':o.side,'qty':str(o.qty),'filled_qty':str(o.filled_qty),'status':o.status}
            for o in self.open_snapshot] if readable and self.engine._broker_snapshot_valid
            and self.open_snapshot is not None else None)
        self.report['CTVA_SPHD'] = {s:self.report['final_positions'].get(s,'0')
                                   if self.report['final_positions'] is not None else None for s in ('CTVA','SPHD')}
        self.report['exposure_flat'] = (self.report['final_positions'] == {} and self.report['final_open_orders'] == []
            and not self.j.owned_qty() and not self.report['unresolved_intents'] and self.controls_valid
            and not self.engine.gate.journal_failed)
        self.report['flat_gate'] = (self.report['final_positions'] == {} and self.report['final_open_orders'] == []
            and not self.j.owned_qty() and not self.report['unresolved_intents'] and not breakers
            and self.controls_valid and not self.engine.gate.journal_failed)
        write_report(self.report_path, self.report)
        self.j.set_meta('paper_auto_state', json.dumps({'entry_gate_open':self.entry_open,
            'status':self.report['status'],'session':self.report['session']}))
        self.last_report = now

    def _graceful_shutdown(self, reason):
        self.entry_open = False
        complete = self.engine.shutdown() if self.schedule else False
        if complete:
            self._controls(force=True)
            self.engine.reconcile(self.clock.now(), side_effects=False)
            self._check_foreign()
        if not complete:
            self._fail('shutdown_incomplete', 'paper-auto did not confirm flat/no orders')
        # Exit status reflects exposure safety: a latched self-healing breaker (clock, REST blip) is not
        # exposure and resolves with fresh evidence in the next process; manual breakers still fail closed.
        manual = [b for b in self.j.breakers() if not breaker_policy(b[0])]
        self.report['status'] = 'SAFE' if complete and not manual and not self.kill_latched else 'FAILED CLOSED'
        self._phase('FINISHED')
        self._persist(final=True)
        if not complete or self.report['status'] != 'SAFE':
            raise FatalRunnerError(RuntimeError('paper-auto shutdown incomplete'), self.report)
        return self.report

    def _fatal_shutdown(self, exc):
        self.entry_open = False
        self.failed = True
        self.report['status'] = 'FAILED CLOSED'
        self.report['failure_reason'] = type(exc).__name__
        result = super()._fatal_shutdown(exc)
        try:
            self._persist(readable=False, final=True)
        except Exception:
            self.report['final_positions'] = None
            self.report['final_open_orders'] = None
            self.report['flat_gate'] = False
            try:
                write_report(self.report_path, self.report)
            except OSError:
                pass
        return result

    def _run(self, until=None):
        now = self.clock.now()
        cal = self._calendar(now)
        session = cal.today(now)
        if session is None or now >= session.close:
            session = cal.next_session(now.astimezone(NY).date(), include_today=False)
        if session is None:
            raise SafetyError('broker calendar has no next valid session')
        self.target = session.day
        # ponytail: one calendar session per launch; a new run revalidates every gate.
        return super()._run(until=until or session.close + timedelta(minutes=3))
