"""Deterministic after-close review of the 10:00 gap-up SHADOW portfolios. Read-only; no model call, no rule change.

One JSON + Markdown report per elapsed session in runtime/reports/deterministic/gapup-review-<day>.*.
A session the collector did not observe is reported as NOT_COLLECTED (incomplete), never replayed.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import fcntl
import json
from pathlib import Path
import sqlite3

from .config import load_settings
from .daily_gate import CLOSE_BUFFER
from .dashboard.gapup import GAPUP, LABELS, lane_metrics, quality, relabel
from .operational_review import atomic_json
from .shadow_analytics import portfolio

NOTE = ("Forward SHADOW on observed IEX quotes: hypothetical fills, no depth or queue proof. The October "
        "backtest is history, not validation. Qualification and promotion are blocked; PAPER stays disabled.")


def _obs(root, day):
    path = Path(root) / 'gapup_v1/observations.sqlite'
    if not path.is_file():
        return None, None
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute("SELECT body FROM diagnostics WHERE day=? AND kind='gapup_session'", (str(day),)).fetchone()
        prov = db.execute("SELECT value FROM meta WHERE key='provenance'").fetchone()
    return (json.loads(row[0]) if row else None), (json.loads(prov[0]) if prov else None)


GAP_SECONDS = 90          # idle ticks come every 20 s; a longer silence means sleep, crash or no network


def collector_gaps(root, day):
    """Silences in the collector's own tick record between 09:37 and 16:00 ET. Read-only evidence of sleep/outage."""
    path = Path(root) / 'gapup_v1/observations.sqlite'
    if not path.is_file():
        return []
    from .calendar import NY
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        times = sorted(datetime.fromisoformat(json.loads(b)['received_at']).astimezone(NY) for (b,) in db.execute(
            "SELECT body FROM diagnostics WHERE day=? AND kind LIKE 'tick:%'", (str(day),)))
    if not times:
        return []
    start, end = datetime.combine(day, datetime.min.time().replace(hour=9, minute=37), tzinfo=NY), \
        datetime.combine(day, datetime.min.time().replace(hour=15, minute=57), tzinfo=NY)
    edges = [start] + times + [end]
    return [{'from': a.isoformat(), 'to': b.isoformat(), 'seconds': round((b - a).total_seconds())}
            for a, b in zip(edges, edges[1:]) if (b - a).total_seconds() > GAP_SECONDS]


def build_report(root, day):
    root = Path(root)
    session, prov = _obs(root, day)
    gp, base = root / 'gapup_v1', root / 'candidate_b/paired-v1'
    report = {'day': str(day), 'observed_at': datetime.now(timezone.utc).isoformat(), 'version': VERSION_OF(prov),
              'session': session or {'day': str(day), 'status': 'NOT_COLLECTED', 'data_gaps': [
                  {'reason': 'no collector session record: machine off, collector down or day before freeze'}]},
              'collector_gaps': collector_gaps(root, day),
              'portfolios': {}, 'orb_same_day': {}, 'qualification': NOTE, 'automatic_rule_change': False}
    for name in GAPUP:
        path = gp / f'{name}.sqlite'
        full = portfolio(path)
        full['closed_trips'] = relabel(full['closed_trips'])
        day_trips = [t for t in full['closed_trips'] if t['day'] == str(day)]
        state = {}
        if path.is_file():
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                row = db.execute("SELECT body FROM diagnostics WHERE day=? AND kind='gapup_state'", (str(day),)).fetchone()
                state = json.loads(row[0]) if row else {}
        decisions = state.get('decisions', {})
        reasons = {}
        for d in decisions.values():
            if not d['eligible']:
                reasons[d['reason']] = reasons.get(d['reason'], 0) + 1
        entries = state.get('entries', {})
        report['portfolios'][name] = {
            'label': LABELS[name], 'decided': state.get('decided', False), 'inputs_ok': state.get('inputs_ok'),
            'inputs_reason': state.get('reason'), 'upstream_scan_at': state.get('upstream_scan_at'),
            'candidates_seen': len(decisions), 'eligible': state.get('eligible', []),
            'filtered_reasons': reasons, 'entries': entries,
            'rejected': {s: e['reason'] for s, e in entries.items() if e.get('status') == 'rejected'},
            'quote_quality': state.get('quality', {}), 'unresolved_open': state.get('unresolved_open', {}),
            'decision_detail': decisions,
            'fills': [f for f in full['fills'] if f['day'] == str(day)],
            'closed_trades': [{k: (str(v) if not isinstance(v, (int, float, str, type(None))) else v)
                               for k, v in t.items() if k not in ('fills', 'order_ids')} for t in day_trips],
            'day_metrics': lane_metrics(day_trips), 'cumulative_metrics': lane_metrics(full['closed_trips']),
            'data_quality_total': quality(path)}
    for name in ('baseline_a', 'candidate_b'):
        lane = portfolio(base / f'{name}.sqlite')
        report['orb_same_day'][name] = {'label': LABELS[name], 'metrics': lane_metrics(
            [t for t in lane['closed_trips'] if t['day'] == str(day)])}
    return report


def VERSION_OF(prov):
    return ((prov or {}).get('contract') or {}).get('version')


def render(report):
    lines = [f"# Gap-up 10:00 SHADOW review {report['day']}", "",
             f"Session {report['session']['status']}. Version {report['version']}. {NOTE}", ""]
    if report['collector_gaps']:
        lines.append(f"**UNRELIABLE DAY: the collector went silent {len(report['collector_gaps'])} time(s) "
                     f"(longest {max(g['seconds'] for g in report['collector_gaps'])} s). Stops and exits may have been checked late. "
                     "Do not count this day toward the 5-session review.**")
    for gap in report['session'].get('data_gaps', []):
        lines.append(f"- Data gap: {gap.get('portfolio', 'all')} {gap.get('symbol', '')} {gap['reason']}")
    for name, p in report['portfolios'].items():
        m, c = p['day_metrics'], p['cumulative_metrics']
        lines += ["", f"## {p['label']}",
                  f"Candidates seen {p['candidates_seen']}; eligible {len(p['eligible'])} ({', '.join(p['eligible']) or 'none'}); "
                  f"rejected at entry: {p['rejected'] or 'none'}.",
                  f"Day: {m['trades']} closed, net 1x {m['net_1x']:+.2f}, net 2x {m['net_2x']:+.2f}.",
                  f"Cumulative: {c['trades']} closed, net 1x {c['net_1x']:+.2f}, net 2x {c['net_2x']:+.2f}, "
                  f"drawdown {c['max_drawdown']:.2f}, profit factor {c['profit_factor']}, "
                  f"95% interval per trade {c['ci95_mean_per_trade']}"
                  + (" (under 30 trades: not meaningful)" if c['small_sample'] else "") + ".",
                  f"Quote checks: {p['quote_quality'] or 'none'}."]
    lines += ["", "## ORB lanes, same day"]
    lines += [f"- {o['label']}: {o['metrics']['trades']} closed, net 1x {o['metrics']['net_1x']:+.2f}, "
              f"net 2x {o['metrics']['net_2x']:+.2f}" for o in report['orb_same_day'].values()]
    return "\n".join(lines) + "\n"


def write_report(reports, report):
    stem = Path(reports) / f"gapup-review-{report['day']}"
    atomic_json(stem.with_suffix('.json'), report)
    tmp = stem.with_suffix('.md.tmp')
    tmp.write_text(render(report))
    tmp.replace(stem.with_suffix('.md'))


def due_days(client, reports, since, now):
    from .calendar import session_from_alpaca
    resp = client.trading_get('/v2/calendar', params={'start': since.isoformat(), 'end': now.date().isoformat()},
                              purpose='gapup_review:calendar')
    if not resp.ok or not isinstance(resp.data, list):
        raise RuntimeError('review calendar unavailable; no session completion claim')
    done = lambda d: (Path(reports) / f'gapup-review-{d}.json').is_file() and (Path(reports) / f'gapup-review-{d}.md').is_file()
    return sorted(s.day for s in map(session_from_alpaca, resp.data) if s.close + CLOSE_BUFFER <= now and not done(s.day))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config/settings.toml')
    parser.add_argument('--env-file', default='.env')
    parser.add_argument('--day', type=date.fromisoformat)
    parser.add_argument('--pending', action='store_true')
    args = parser.parse_args()
    settings = load_settings(args.config)
    root = Path(settings.data_dir)
    reports = root / 'reports/deterministic'
    reports.mkdir(parents=True, exist_ok=True)
    with (reports / 'gapup-review.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        if args.day:
            days = [args.day]
        else:
            from .http import ReadOnlyClient
            from .secrets import load_credentials, load_env_file
            load_env_file(args.env_file)
            _, prov = _obs(root, date.today())
            since = date.fromisoformat((prov or {}).get('frozen_at', '')[:10]) if prov else None
            if since is None:
                print('gap-up collector has not frozen provenance yet; nothing to review.')
                return 0
            client = ReadOnlyClient(load_credentials(), settings.trading_calls_per_minute,
                                    settings.market_data_calls_per_minute, timeout_seconds=settings.request_timeout_seconds)
            days = due_days(client, reports, since, datetime.now(timezone.utc))
        for day in days:
            write_report(reports, build_report(root, day))
            print(f'{day}: gap-up review persisted; strategies remain unqualified')
        if not days:
            print('No completed sessions due for gap-up review.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
