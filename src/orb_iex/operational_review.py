"""Deterministic after-close reporting. Read-only broker access; no rule changes."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3

from .candidate_review import review
from .cli import _journal_path
from .config import load_settings
from .daily_gate import CLOSE_BUFFER, close_time, is_trading_day
from .calendar import session_from_alpaca
from .http import ReadOnlyClient
from .paper_state import inspect_paper
from .reconcile import compare_trips, fills_from_journal, reconstruct, totals
from .secrets import load_credentials, load_env_file
from .shadow_analytics import paired_summary


def atomic_json(path, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w') as stream:
        stream.write(json.dumps(body, default=str, indent=2))
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def report_complete(reports, day):
    stem = Path(reports) / f'daily-review-{day}'
    if not stem.with_suffix('.md').is_file() or not stem.with_suffix('.json').is_file():
        return False
    try:
        report = json.loads(stem.with_suffix('.json').read_text())
        return (report['day'] == str(day) and not report['reconciliation']['issues']
                and not report['broker_snapshot']['errors'])
    except (ValueError, KeyError, TypeError):
        return False


def due_sessions(client, reports, now):
    # The operational system began October 26. Query the exchange calendar for
    # every subsequent date, without a rolling lookback or permanent retry cap.
    response = client.trading_get('/v2/calendar', params={
        'start': '2026-10-26', 'end': now.date().isoformat()}, purpose='deterministic_review:calendar')
    if not response.ok or not isinstance(response.data, list):
        raise RuntimeError('review calendar unavailable; no session completion claim')
    sessions = [session_from_alpaca(r) for r in response.data]
    return sorted(s.day for s in sessions if s.close + CLOSE_BUFFER <= now
                  and not report_complete(reports, s.day))


def broker_history(client):
    """Read all order history using overlapping time partitions, deduped by ID.

    A saturated interval is split, never accepted as complete. Overlap protects
    both inclusive and exclusive timestamp boundaries. Unresolvable saturation
    fails closed. No broker mutations or production execution changes.
    """
    def get(start=None, end=None):
        params = {'status': 'all', 'limit': 500, 'direction': 'asc', 'nested': 'false'}
        if start:
            params['after'] = start.isoformat()
        if end:
            params['until'] = end.isoformat()
        response = client.trading_get('/v2/orders', params=params,
            purpose='deterministic_review:current_reconciliation')
        if not response.ok or not isinstance(response.data, list):
            raise RuntimeError('broker order history unavailable')
        return response.data
    rows = get()
    if len(rows) < 500:
        return rows
    earliest = min(datetime.fromisoformat(r['submitted_at']) for r in rows)
    pending = [(earliest - timedelta(seconds=1), datetime.now(timezone.utc) + timedelta(seconds=1))]
    collected = {}
    requests = 0
    while pending:
        start, end = pending.pop()
        rows = get(start, end)
        requests += 1
        if requests > 10000:
            raise RuntimeError('broker history partition limit exceeded; incomplete')
        if len(rows) >= 500:
            if end - start <= timedelta(microseconds=4):
                raise RuntimeError('broker history timestamp saturation; incomplete')
            mid = start + (end - start) / 2
            pending.extend([(start, mid + timedelta(microseconds=1)),
                            (mid - timedelta(microseconds=1), end)])
        else:
            for row in rows:
                collected[row['id']] = row
    return sorted(collected.values(), key=lambda r: (r['submitted_at'], r['id']))


def journal_review(path, day, evidence):
    if not Path(path).is_file():
        return {'status': 'INCOMPLETE', 'reason': 'journal not yet collected',
                'evidence': evidence, 'paired_plans': {}, 'quote_decisions': {}}
    report = review(path, day)
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        fills = [dict(r) for r in db.execute('SELECT * FROM fills WHERE day=?', (day.isoformat(),))]
    closed, opened = reconstruct(fills=fills_from_journal(fills))
    # Keep every same-symbol/day round trip; never aggregate distinct trips away.
    report.update(evidence=evidence, closed_trips=closed, open_trips=opened,
                  excluded_winners='UNKNOWN: paired decisions do not establish counterfactual fills/returns')
    return report


def build_report(client, settings, shadow_settings, day):
    observed = datetime.now(timezone.utc).isoformat()
    state = inspect_paper(client, settings, _journal_path(settings, 'paper'))
    paper = journal_review(_journal_path(settings, 'paper'), day, 'ALPACA_PAPER_RECORDED_FILLS')
    shadow = journal_review(_journal_path(shadow_settings, 'shadow'), day,
                            'FORWARD_IEX_HYPOTHETICAL_QUOTES_NOT_BROKER_FILLS')
    orders = broker_history(client)
    closed, opened = reconstruct(orders)
    with sqlite3.connect(_journal_path(settings, 'paper').resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        jc, jo = reconstruct(fills=fills_from_journal([dict(r) for r in db.execute('SELECT * FROM fills')]))
    issues = compare_trips(closed, jc)
    if opened or jo:
        issues.append({'kind': 'open_trips_require_review', 'broker': len(opened), 'journal': len(jo)})
    return {'version': 1, 'day': day.isoformat(), 'observed_at': observed,
            'configured_collection_mode': 'SHADOW',
            'paper_ready': False, 'paper_entries_paused': Path(settings.kill_switch_file).exists(),
            'broker_snapshot': state,
            'snapshot_scope': 'current broker state, not a reconstruction of the reviewed historical day',
            'reconciliation': {'orders': len(orders), 'closed_trips': len(closed),
                'issues': issues, 'totals': totals(closed)},
            'paper': paper, 'shadow': shadow,
            'paired_shadow': paired_summary(Path(shadow_settings.data_dir).parent, day),
            'execution_cost_change': None,
            'cost_note': 'No measured cost reduction. Gross already contains paper fill effects; overlays are additional models.',
            'qualification': 'BLOCKED: untouched >=60 sessions AND >=200 trades after freeze; full point-in-time execution evidence, absolute drawdown limit and supervised PAPER acceptance/approval required.',
            'automatic_promotion': False}


def write_report(root, report):
    root = Path(root)
    stem = root / f"daily-review-{report['day']}"
    atomic_json(stem.with_suffix('.json'), report)
    reconciliation = report['reconciliation']
    text = (f"# Deterministic ORB review {report['day']}\n\n"
            f"Observed {report['observed_at']}. PAPER READY: false.\n\n"
            f"Current broker reconciliation: {reconciliation['closed_trips']} closed trips; "
            f"{len(reconciliation['issues'])} issues. Historical review evidence: PAPER "
            f"{report['paper']['status']}, SHADOW {report['shadow']['status']}.\n\n"
            f"{report['cost_note']}\n\n{report['qualification']}\n\n"
            "The JSON preserves all observed decisions, exclusions, outcome/process evidence, "
            "and normal/doubled modeled costs. Missing outcomes remain unknown. "
            "No automatic rule change or model call.\n")
    temp = stem.with_suffix('.md.tmp')
    with temp.open('w') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(stem.with_suffix('.md'))


def publish_latest(reports, data_dir):
    files = sorted(Path(reports).glob('daily-review-????-??-??.json'))
    if files:
        atomic_json(Path(data_dir) / 'health/operational-review.json', json.loads(files[-1].read_text()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config/settings.toml')
    parser.add_argument('--shadow-config', default='config/candidate_b.toml')
    parser.add_argument('--env-file', default='.env')
    parser.add_argument('--day', type=date.fromisoformat)
    parser.add_argument('--pending', action='store_true')
    args = parser.parse_args()
    if args.day and (not is_trading_day(args.day)
                     or datetime.now(timezone.utc) < close_time(args.day) + CLOSE_BUFFER):
        parser.error('review requires a completed trading session plus its close buffer')
    settings, shadow = load_settings(args.config), load_settings(args.shadow_config)
    reports = Path(settings.data_dir) / 'reports' / 'deterministic'
    reports.mkdir(parents=True, exist_ok=True)
    with (reports / 'review.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        load_env_file(args.env_file)
        client = ReadOnlyClient(load_credentials(), settings.trading_calls_per_minute,
                                settings.market_data_calls_per_minute,
                                timeout_seconds=settings.request_timeout_seconds)
        days = ([args.day] if not report_complete(reports, args.day) else []) if args.day else due_sessions(
            client, reports, datetime.now(timezone.utc))
        if not days:
            publish_latest(reports, settings.data_dir)
            print('No completed sessions due for deterministic review.')
            return 0
        for day in days:
            report = build_report(client, settings, shadow, day)
            if report['broker_snapshot']['errors'] or report['reconciliation']['issues']:
                atomic_json(reports / f'failed-{day}.json', report)
                raise RuntimeError('broker verification/reconciliation incomplete; session remains due')
            write_report(reports, report)
            print(f"{day}: deterministic review persisted; strategy remains unqualified")
        publish_latest(reports, settings.data_dir)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
