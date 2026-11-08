"""One local A/B collector. Immutable inputs, independent atomic SHADOW ledgers.

No broker order client, arming method, bar fill model or automatic promotion.
The observation journal is write-ahead: pending ticks reuse recorded responses
after a crash. Each portfolio commits its broker checkpoint and engine journal
together. The append-only paper and legacy shadow journals are never replaced.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import queue
import time

from .calendar import NY
from .journal import Journal
from .marketdata import Bar, Quote, load_calendar
from .ops import write_heartbeat
from .shadow_session import ForwardShadowBroker, ForwardShadowRunner

CHECKPOINT = 'forward_checkpoint'
START_DAY = date(2026, 10, 29)


def encode(value):
    if isinstance(value, Decimal):
        return {'$decimal': str(value)}
    if isinstance(value, datetime):
        return {'$datetime': value.isoformat()}
    if isinstance(value, date):
        return {'$date': value.isoformat()}
    if is_dataclass(value):
        return {'$type': type(value).__name__, 'fields': encode(asdict(value))}
    if isinstance(value, dict):
        return {'$map': [[encode(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, (list, tuple)):
        return [encode(x) for x in value]
    return value


def decode(value):
    if isinstance(value, list):
        return [decode(x) for x in value]
    if not isinstance(value, dict):
        return value
    if '$decimal' in value:
        return Decimal(value['$decimal'])
    if '$datetime' in value:
        return datetime.fromisoformat(value['$datetime'])
    if '$date' in value:
        return date.fromisoformat(value['$date'])
    if '$map' in value:
        return {decode(k): decode(v) for k, v in value['$map']}
    if '$type' in value:
        cls = {'Bar': Bar, 'Quote': Quote}.get(value['$type'])
        if cls is None:
            raise ValueError('unsupported observation type')
        return cls(**decode(value['fields']))
    raise ValueError('malformed observation')


def dumps(value):
    return json.dumps(value, default=str, sort_keys=True, allow_nan=False)


class ObservationClock:
    """Both portfolios reason at the recorded reception time, including replay."""
    def __init__(self):
        self.current = datetime.now(timezone.utc)
        self.live = False

    def now(self):
        return self.current

    def sleep(self, seconds):
        # Only brief opening-bar retries use this; the coordinator sleeps outside
        # transactions. A response then advances to its actual reception time.
        if self.live:
            time.sleep(seconds)
        self.current += timedelta(seconds=seconds)


class SharedObservations:
    def __init__(self, data, journal, clock, wall_now=None):
        self.data, self.journal, self.clock = data, journal, clock
        self.wall_now = wall_now or (lambda: datetime.now(timezone.utc))
        self.tick = None

    def begin(self, tick):
        self.tick = tick
        self.clock.current = datetime.fromisoformat(tick['at'])
        self.clock.live = False
        self.calls = {}

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        method = getattr(self.data, name)
        if not callable(method):
            return method

        def observed(*args, **kwargs):
            if name == 'entry_quotes':
                # Both profiles see one immutable quote per symbol per tick,
                # including B exclusions and the other profile's open orders.
                symbols = args[0]
                return self._quotes(method, symbols)
            key = hashlib.sha256(dumps(encode([name, args, kwargs])).encode()).hexdigest()
            attempt = self.calls.get(key, 0)
            self.calls[key] = attempt + 1
            return self._call(f'{key}:{attempt}', method, args, kwargs)
        return observed

    def _call(self, key, method, args, kwargs):
        day = datetime.fromisoformat(self.tick['at']).astimezone(NY).date()
        kind = f"input:{self.tick['id']}:{key}"
        record = self.journal.diagnostic(day, kind)
        if record is None:
            self.clock.live = True
            try:
                value = method(*args, **kwargs)
                record = {'received_at': self.wall_now().isoformat(), 'value': encode(value)}
            except Exception as exc:
                record = {'received_at': self.wall_now().isoformat(),
                          'error': f'{type(exc).__name__}: {exc}'}
            self.journal.save_diagnostic(day, kind, record)
        self.clock.current = max(self.clock.current, datetime.fromisoformat(record['received_at']))
        if 'error' in record:
            raise ConnectionError(record['error'])
        return decode(record['value'])

    def _quotes(self, method, symbols):
        # The first request normally covers baseline top-20, a superset of B.
        # Missing symbols can be obtained once without replacing earlier input.
        day = datetime.fromisoformat(self.tick['at']).astimezone(NY).date()
        kind = f"quotes:{self.tick['id']}"
        record = self.journal.diagnostic(day, kind) or {'values': {}, 'times': {}}
        missing = [s for s in symbols if s not in record['values']]
        if missing:
            self.clock.live = True
            try:
                got = method(missing, self.wall_now())
                error = None
            except Exception as exc:
                got, error = {}, f'{type(exc).__name__}: {exc}'
            received = self.wall_now().isoformat()
            for s in missing:
                record['values'][s] = encode(got.get(s))
                record['times'][s] = received
            if error:
                record.setdefault('errors', []).append(error)
            self.journal.save_diagnostic(day, kind, record)
        for s in symbols:
            self.clock.current = max(self.clock.current, datetime.fromisoformat(record['times'][s]))
        return {s: decode(record['values'][s]) for s in symbols}


def restore_broker(journal, broker):
    raw = journal.get_meta(CHECKPOINT)
    if raw is None:
        if journal.orders() or journal.db.execute('SELECT 1 FROM fills LIMIT 1').fetchone():
            raise ValueError('SHADOW ledger exists without a checkpoint; operator review required')
        return
    body = json.loads(raw)
    if body.get('version') != 1 or type(body.get('tick')) is not int:
        raise ValueError('invalid SHADOW checkpoint envelope')
    broker.restore(body['broker'])
    # Detect corruption or any partial/manual state mutation before recovering.
    fills = journal.db.execute('SELECT symbol,side,qty,price FROM fills').fetchall()
    positions, cash = {}, Decimal(0)
    for symbol, side, qty, price in fills:
        signed = Decimal(qty) * (1 if side == 'buy' else -1)
        positions[symbol] = positions.get(symbol, Decimal(0)) + signed
        cash -= signed * Decimal(price)
    positions = {s: q for s, q in positions.items() if q}
    # Cumulative VWAP division can differ at Decimal's final digit. This is
    # strictly a sub-cent arithmetic tolerance, never an inventory tolerance.
    if positions != broker.list_positions() or abs(cash - broker.cash_pnl) > Decimal('0.000000000001'):
        raise ValueError('SHADOW checkpoint disagrees with durable fills')
    orders = journal.orders()
    if set(broker.orders) != {o.order_id for o in orders}:
        raise ValueError('SHADOW checkpoint disagrees with durable orders')
    if any((o.status, o.qty, o.filled_qty, o.client_order_id) !=
           (broker.orders[o.order_id].status, broker.orders[o.order_id].qty,
            broker.orders[o.order_id].filled_qty, broker.orders[o.order_id].client_order_id) for o in orders):
        raise ValueError('SHADOW checkpoint disagrees with order quantities/status')


def atomic_step(runner, tick):
    previous = runner.j.get_meta(CHECKPOINT)
    if previous and json.loads(previous)['tick'] >= tick['id']:
        return 5.0
    # In-memory state is discarded by process restart if this transaction fails.
    # No TradeLog writes occur: this state's fills are the authoritative SHADOW
    # ledger and the read-only journal derives its views directly from them.
    with runner.j.transaction():
        wait = runner.step()
        runner._drain_events(runner.clock.now())
        runner.j.set_meta(CHECKPOINT, dumps({'version': 1, 'tick': tick['id'],
                         'at': runner.clock.now().isoformat(), 'broker': runner.broker.checkpoint()}))
    return wait


def session_ledger(observations, runners, calendar, now):
    """Durable unique record for every elapsed exchange session after freeze.

    Offline gaps are INCOMPLETE, never retroactively replayed as live evidence.
    """
    provenance = json.loads(observations.get_meta('provenance') or '{}')
    frozen = datetime.fromisoformat(provenance['frozen_at']) if provenance else None
    for session in calendar._sessions:
        if not START_DAY <= session.day <= now.astimezone(NY).date() or now < session.close:
            continue
        previous = observations.diagnostic(session.day, 'paired_session')
        if previous and previous['status'] == 'COMPLETE':
            continue
        states = [r.j.day(session.day) for r in runners]
        gaps = []
        for r in runners:
            for kind, body in r.j.db.execute("SELECT kind,body FROM diagnostics WHERE day=? "
                    "AND (kind LIKE 'forward_gap:%' OR kind LIKE 'ab_quote:%')", (str(session.day),)):
                body = json.loads(body)
                unavailable = body.get('baseline_reason') in (
                    'skip_quote_unavailable', 'stale_quote', 'quote_no_timestamp', 'quote_from_future')
                missing_size = body.get('ab', {}).get('candidate_reason') == 'candidate_displayed_size' \
                    and body.get('ab', {}).get('entry_size_round_lots') is None
                if kind.startswith('forward_gap:') or unavailable or missing_size:
                    gaps.append({'profile': r.settings.research_profile, 'kind': kind,
                                 'reason': body.get('reason') or body.get('baseline_reason') or 'missing_displayed_size'})
        completed = (all('post_close' in s['done'] and s['scan_status'] == 'ok' for s in states)
                     and (frozen is None or session.open > frozen) and not gaps)
        flat = all(not r.broker.list_positions() for r in runners)
        if now < session.close + timedelta(minutes=1) and not completed:
            continue
        if previous and not (completed and flat):
            continue
        observations.save_diagnostic(session.day, 'paired_session', {
            'day': str(session.day), 'status': 'COMPLETE' if completed and flat else 'INCOMPLETE',
            'reason': None if completed and flat else 'missed/failed scan, unfinished session or open hypothetical exposure',
            'observed_at': now.isoformat(), 'scan_status': [s['scan_status'] for s in states],
            'processed': True, 'data_gaps': gaps,
            'evidence': 'OBSERVED_IEX_HYPOTHETICAL; no broker fills or depth proof'})


def bind_provenance(observations, settings, equity):
    source = Path(__file__).parent
    files = ['shadow_pair.py', 'shadow_session.py', 'runner.py', 'engine.py',
             'strategy.py', 'candidate_b.py', 'sim.py', 'marketdata.py', 'config.py',
             'journal.py', 'execution.py', 'orders.py', 'calendar.py', 'constants.py',
             'shadow_analytics.py', 'candidate_review.py', 'operational_review.py']
    hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in files}
    contract = {'files': hashes, 'settings': asdict(settings)}
    previous = observations.get_meta('provenance')
    if previous:
        previous = json.loads(previous)
        if previous['contract'] != json.loads(dumps(contract)):
            raise ValueError('SHADOW mechanism/config changed after freeze; preserve ledger and obtain new provenance approval')
        return Decimal(previous['initial_equity'])
    if observations.get_meta('tick'):
        raise ValueError('existing observations lack provenance')
    observations.set_meta('provenance', dumps({'version': 1, 'contract': contract,
        'frozen_at': datetime.now(timezone.utc).isoformat(), 'initial_equity': str(equity),
        'qualification': 'UNQUALIFIED; >=60 complete sessions AND >=200 B trades; separate approval'}))
    return equity


def main():
    from . import cli
    from .http import ReadOnlyClient
    from .marketdata import MarketData, BarCache
    from .ops import InstanceLock, check_disk
    from .preflight import verify_environment
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config/candidate_b.toml')
    parser.add_argument('--env-file', default='.env')
    args = parser.parse_args()
    args.require_private_env = True
    settings, credentials, _ = cli._prepare(args)
    if settings.research_profile != 'candidate_b' or settings.enable_paper_orders:
        parser.error('paired collector requires disabled Candidate B configuration')
    root = Path(settings.data_dir)
    check_disk(root, settings.min_free_disk_mb)
    lock = InstanceLock(root).acquire()
    cli._graceful_sigterm()
    try:
        client = ReadOnlyClient(credentials, settings.trading_calls_per_minute,
            settings.market_data_calls_per_minute, timeout_seconds=settings.request_timeout_seconds)
        summary = verify_environment(client, settings, 'shadow')
        # A fresh versioned subtree preserves every legacy SHADOW record.
        paired = root / 'paired-v1'
        paired.mkdir(parents=True, exist_ok=True)
        observations = Journal(paired / 'observations.sqlite')
        equity = bind_provenance(observations, settings, Decimal(summary['equity']))
        clock = ObservationClock()
        data = MarketData(client, settings, BarCache(root / 'cache.sqlite'))
        data.on_progress = lambda: write_heartbeat(root, {
            'mode': 'shadow', 'phase': 'collecting_inputs', 'paired': True,
            'health': 'healthy', 'health_reasons': [],
            'bot_time': datetime.now(timezone.utc).isoformat()})
        shared = SharedObservations(data, observations, clock)
        runners = []
        for profile in ('baseline_a', 'candidate_b'):
            lane = replace(settings, research_profile=profile, data_dir=str(paired / profile),
                           journal_dir=str(paired / profile / 'journal'))
            j = Journal(paired / f'{profile}.sqlite')
            events = queue.Queue()
            broker = ForwardShadowBroker(events.put, equity=equity, clock=clock.now,
                max_age=settings.max_quote_age_seconds, max_spread=settings.max_quote_spread_pct)
            restore_broker(j, broker)
            r = ForwardShadowRunner(lane, 'shadow', clock, client, shared, j,
                broker=broker, events=events, account_source=broker.get_account)
            runners.append(r)
        calendar_day = None
        while True:
            now = datetime.now(timezone.utc)
            if calendar_day != now.astimezone(NY).date():
                # Catch up every elapsed session since this prospective freeze,
                # even after an outage longer than the default 90-day window.
                back = max(90, (now.astimezone(NY).date() - START_DAY).days + 21)
                cal = load_calendar(client, now.astimezone(NY).date(), back_days=back)
                for r in runners:
                    r.calendar, r.calendar_loaded_for = cal, date.max
                calendar_day = now.astimezone(NY).date()
            pending = observations.get_meta('tick')
            old = json.loads(pending) if pending else None
            tick = old if old and not old['complete'] else {
                'id': (old['id'] + 1 if old else 1), 'at': now.isoformat(), 'complete': False}
            observations.set_meta('tick', dumps(tick))
            waits = []
            for r in runners:
                shared.begin(tick)
                waits.append(atomic_step(r, tick))
            tick['complete'] = True
            observations.set_meta('tick', dumps(tick))
            session_ledger(observations, runners, cal, now)
            beats = [r._health(clock.now()) for r in runners]
            write_heartbeat(root, {'mode': 'shadow', 'phase': runners[1].schedule.phase(clock.now()),
                'session': str(runners[1].schedule.session.day), 'bot_time': clock.now().isoformat(),
                'last_data_update': max([r.last_data_update or '' for r in runners]) or None,
                'health': 'healthy' if all(b['health'] == 'healthy' for b in beats) else 'degraded',
                'health_reasons': [x for b in beats for x in b['health_reasons']],
                'paired': True, 'tick': tick['id'], 'profiles': ['baseline_a', 'candidate_b']})
            time.sleep(max(0.2, min(waits + [30.0])))
    except KeyboardInterrupt:
        # No simulated EOD on service stop. Last complete transaction is restored.
        return 0
    finally:
        lock.release()


if __name__ == '__main__':
    raise SystemExit(main())
