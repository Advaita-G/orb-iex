from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sys

import pytest

from orb_iex.dashboard.operations import snapshot
from orb_iex.journal import Journal
from orb_iex.operational_review import journal_review, publish_latest, atomic_json
from orb_iex.operational_review import due_sessions, report_complete, broker_history
from orb_iex.http import ApiResponse


def test_missing_shadow_journal_stays_unknown_and_is_not_created(tmp_path):
    p = tmp_path / 'shadow.sqlite'
    result = journal_review(p, date(2026, 10, 29), 'HYPOTHETICAL')
    assert result['status'] == 'INCOMPLETE' and not p.exists()
    assert snapshot(tmp_path)['runtime_mode'] == 'BLOCKED'


def test_review_preserves_two_round_trips_for_same_symbol_day_readonly(tmp_path):
    day = date(2026, 10, 29)
    p = tmp_path / 'shadow.sqlite'
    j = Journal(p)
    for idx, side, role, price in [(1, 'buy', 'entry', '10'), (2, 'sell', 'stop1', '9'),
                                    (3, 'buy', 'entry', '10'), (4, 'sell', 'eod1', '12')]:
        j.db.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (str(idx), '10', str(day), 'AAA', role, side, '10', price,
             f'2026-10-29T13:3{idx}:00+00:00', '0.035', '0.10'))
    j.db.close()
    before = hashlib.sha256(p.read_bytes()).hexdigest()
    result = journal_review(p, day, 'HYPOTHETICAL')
    assert [t['gross_pnl'] for t in result['closed_trips']] == [-10, 20]
    assert len(result['closed_trips']) == 2 and result['open_trips'] == []
    assert result['excluded_winners'].startswith('UNKNOWN')
    assert hashlib.sha256(p.read_bytes()).hexdigest() == before


def test_collector_state_needs_a_fresh_actual_shadow_heartbeat(tmp_path):
    hp = tmp_path / 'candidate_b/health/heartbeat.json'
    hp.parent.mkdir(parents=True)
    for age, mode, expected in [(0, 'shadow', 'SHADOW'), (500, 'shadow', 'BLOCKED'),
                                 (0, 'paper', 'BLOCKED')]:
        hp.write_text(json.dumps({'mode': mode, 'pid': os.getpid(), 'updated_at':
            (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()}))
        assert snapshot(tmp_path)['runtime_mode'] == expected
    hp.write_text(json.dumps({'mode': 'shadow', 'pid': 999999999,
                             'updated_at': datetime.now(timezone.utc).isoformat()}))
    assert snapshot(tmp_path)['runtime_mode'] == 'BLOCKED'


def test_catchup_reviews_cannot_regress_the_latest_session(tmp_path):
    reports = tmp_path / 'reports'
    for day in ('2026-10-28', '2026-10-26', '2026-10-27'):
        atomic_json(reports / f'daily-review-{day}.json', {'day': day})
    publish_latest(reports, tmp_path)
    assert json.loads((tmp_path / 'health/operational-review.json').read_text())['day'] == '2026-10-28'


def test_future_session_review_is_refused_before_credentials_or_reports(monkeypatch):
    from orb_iex.operational_review import main
    monkeypatch.setattr(sys, 'argv', ['operational_review', '--day', '2099-01-02'])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_launchd_uses_one_canonical_shadow_one_paper_and_loopback_journal():
    root = Path(__file__).resolve().parents[2]
    def plist(name):
        return plistlib.loads((root / 'deploy/macos' / name).read_bytes())
    shadow = plist('com.orbiex.shadow.plist')
    assert 'orb_iex.shadow_session' in shadow['ProgramArguments']
    assert shadow['KeepAlive'] is True
    assert 'paper' not in shadow['ProgramArguments']
    journal = plist('com.orbiex.journal.plist')
    assert '127.0.0.1' in journal['ProgramArguments']
    review = plist('com.orbiex.daily-review.plist')
    assert 'orb_iex.operational_review' in review['ProgramArguments']
    assert '--pending' in review['ProgramArguments']
    assert not any('claude' in a for a in review['ProgramArguments'])


def test_exchange_calendar_catchup_has_no_three_day_or_retry_limit(tmp_path):
    from helpers import calendar
    class Client:
        def trading_get(self, path, **kwargs):
            assert path == '/v2/calendar'
            return ApiResponse(200, [{'date': str(s.day), 'open': '09:30', 'close': '16:00'}
                for s in calendar(start=date(2026,10,26),end=date(2026,11,9))._sessions], {}, '')
    day = date(2026, 10, 26)
    (tmp_path / f'daily-review-{day}.attempts').write_text('100')
    due = due_sessions(Client(), tmp_path, datetime(2026,11,9,23,tzinfo=timezone.utc))
    assert due[0] == day and due[-1] == date(2026,11,9) and len(due) == 11
    report = {'day': str(day), 'reconciliation': {'issues': []}, 'broker_snapshot': {'errors': []}}
    atomic_json(tmp_path / f'daily-review-{day}.json', report)
    assert not report_complete(tmp_path, day)  # crash between JSON and Markdown
    (tmp_path / f'daily-review-{day}.md').write_text('complete')
    assert report_complete(tmp_path, day)
    assert day not in due_sessions(Client(), tmp_path, datetime(2026,11,9,23,tzinfo=timezone.utc))


def test_review_broker_history_splits_saturated_intervals_without_dropping_boundaries():
    start = datetime(2026,10,1,tzinfo=timezone.utc)
    rows = [{'id': str(i), 'submitted_at': (start + timedelta(seconds=i)).isoformat()} for i in range(1200)]
    class Client:
        calls = 0
        def trading_get(self, path, params, **kwargs):
            self.calls += 1
            selected = rows
            if params.get('after'):
                selected = [r for r in selected if r['submitted_at'] > params['after']]
            if params.get('until'):
                selected = [r for r in selected if r['submitted_at'] < params['until']]
            return ApiResponse(200, selected[:500], {}, '')
    c = Client()
    assert broker_history(c) == rows and c.calls > 1


def test_unavailable_broker_history_cannot_be_reported_as_empty():
    class Client:
        def trading_get(self, *args, **kwargs):
            return ApiResponse(503, {}, {}, '')
    with pytest.raises(RuntimeError, match='unavailable'):
        broker_history(Client())
