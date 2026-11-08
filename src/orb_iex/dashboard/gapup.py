"""Read-only comparison of the 10:00 gap-up SHADOW portfolios with the ORB lanes.

Same ledgers, same cost convention (fills at observed ask/bid plus 1x and 2x modeled per-share costs).
Nothing here writes, qualifies or promotes anything. Forward results only; the October research is history.
"""
from collections import Counter, defaultdict
from decimal import Decimal as D
import json
from pathlib import Path
import random
import sqlite3
from contextlib import closing

from ..shadow_analytics import portfolio

GAPUP = ('gapup_top10', 'gapup_top5')
LABELS = {'gapup_top10': 'Top-10 gap-up 10:00', 'gapup_top5': 'Top-5 gap-up 10:00',
          'baseline_a': 'ORB baseline A (frozen)', 'candidate_b': 'ORB Candidate B'}


def lane_metrics(trips, since=None):
    """trips: closed trips from shadow_analytics.portfolio. Metrics on modeled net P&L (1x) and 2x costs."""
    rows = [t for t in trips if since is None or t['day'] >= since]
    rows.sort(key=lambda t: (t['exit_time'] or '', t['symbol']))
    n = len(rows)
    out = {'trades': n, 'since': since}
    if not n:
        return out | {'net_1x': 0.0, 'net_2x': 0.0, 'gross': 0.0, 'expectancy_1x': None, 'win_rate': None,
                      'profit_factor': None, 'max_drawdown': 0.0, 'ci95_mean_per_trade': None,
                      'best_symbol': None, 'best_day': None, 'small_sample': True}
    net = [t['net_modeled_pnl'] for t in rows]
    net2 = [t['modeled_net_2x'] for t in rows]
    wins, losses = sum(x for x in net if x > 0), -sum(x for x in net if x <= 0)
    eq = peak = dd = D(0)
    for x in net:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    by_sym, by_day = defaultdict(D), defaultdict(D)
    for t in rows:
        by_sym[t['symbol']] += t['net_modeled_pnl']
        by_day[t['day']] += t['net_modeled_pnl']
    total = sum(net)
    bs, bd = max(by_sym.items(), key=lambda kv: kv[1]), max(by_day.items(), key=lambda kv: kv[1])
    ci = None
    if n >= 2:
        rng, vals = random.Random(7), [float(x) for x in net]
        means = sorted(sum(vals[rng.randrange(n)] for _ in range(n)) / n for _ in range(2000))
        ci = [means[50], means[1949]]
    return out | {'net_1x': float(total), 'net_2x': float(sum(net2)), 'gross': float(sum(t['gross_pnl'] for t in rows)),
                  'expectancy_1x': float(total / n), 'win_rate': sum(1 for x in net if x > 0) / n,
                  'profit_factor': float(wins / losses) if losses else None, 'max_drawdown': float(dd),
                  'ci95_mean_per_trade': ci, 'small_sample': n < 30,
                  'best_symbol': {'symbol': bs[0], 'net': float(bs[1]), 'net_without': float(total - bs[1])},
                  'best_day': {'day': bd[0], 'net': float(bd[1]), 'net_without': float(total - bd[1])}}


def relabel(trips):
    """A target exit is a planned strategy outcome; the shared classifier only knows ORB stop/EOD roles."""
    for t in trips:
        if t.get('classification') == 'unknown_process_evidence':
            roles = {f['role'] for f in t.get('fills', []) if f['role'] != 'entry'}
            if roles and all(r.startswith('tgt') for r in roles):
                t['classification'] = 'ordinary_strategy_outcome'
    return trips


def quality(path):
    p = Path(path)
    if not p.is_file():
        return {}
    with closing(sqlite3.connect(p.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        states = [json.loads(r[0]) for r in db.execute("SELECT body FROM diagnostics WHERE kind='gapup_state'")]
        cand = Counter((r[0], r[1]) for r in db.execute("SELECT status,reason FROM candidates"))
    q = Counter()
    for s in states:
        q.update(s.get('quality', {}))
    return {'quote_decisions': dict(q), 'days_decided': sum(1 for s in states if s.get('decided')),
            'days_inputs_failed': sum(1 for s in states if s.get('decided') and not s.get('inputs_ok')),
            'rejected_or_filtered': {f'{s}:{r}': n for (s, r), n in sorted(cand.items(), key=lambda kv: -kv[1])
                                     if s in ('rejected', 'filtered')},
            'submitted': sum(n for (s, _), n in cand.items() if s == 'submitted')}


def summary(root):
    root = Path(root)
    base, gp = root / 'candidate_b/paired-v1', root / 'gapup_v1'
    sessions, prov = [], None
    if (gp / 'observations.sqlite').is_file():
        with closing(sqlite3.connect((gp / 'observations.sqlite').resolve().as_uri() + '?mode=ro', uri=True)) as db:
            sessions = [json.loads(r[0]) for r in db.execute(
                "SELECT body FROM diagnostics WHERE kind='gapup_session' ORDER BY day")]
            row = db.execute("SELECT value FROM meta WHERE key='provenance'").fetchone()
            prov = json.loads(row[0]) if row else None
    since = min((s['day'] for s in sessions), default=None) or (prov or {}).get('frozen_at', '')[:10] or None
    lanes = {}
    for name in GAPUP:
        lane = portfolio(gp / f'{name}.sqlite')
        lane['closed_trips'] = relabel(lane['closed_trips'])
        lanes[name] = lane
    orb = {n: portfolio(base / f'{n}.sqlite') for n in ('baseline_a', 'candidate_b')}
    complete = [s for s in sessions if s['status'] == 'COMPLETE']
    out = {'evidence': 'FORWARD_IEX_HYPOTHETICAL_NOT_BROKER_FILLS', 'since': since,
           'version': ((prov or {}).get('contract') or {}).get('version'), 'provenance': prov,
           'sessions': {'recorded': len(sessions), 'complete': len(complete),
                        'incomplete': [{'day': s['day'], 'gaps': s['data_gaps']} for s in sessions if s['status'] != 'COMPLETE']},
           'portfolios': {}, 'same_period_orb': {}, 'qualification': {
               'qualified': False, 'automatic_promotion': False,
               'note': 'Forward SHADOW only. The October backtest (+$4,116 Top-10, 10:00 entry) is hypothetical history, not validation.'}}
    for name, lane in lanes.items():
        out['portfolios'][name] = {'label': LABELS[name], 'status': lane['status'],
                                   'metrics': lane_metrics(lane['closed_trips']),
                                   'open_positions': lane.get('positions', {}), 'quality': quality(gp / f'{name}.sqlite'),
                                   'trades': [{k: (str(v) if isinstance(v, D) else v) for k, v in t.items()
                                               if k in ('day', 'symbol', 'qty', 'entry_vwap', 'exit_vwap', 'gross_pnl',
                                                        'modeled_cost', 'net_modeled_pnl', 'modeled_net_2x', 'classification')}
                                              for t in lane['closed_trips']]}
    for name, lane in orb.items():
        out['same_period_orb'][name] = {'label': LABELS[name], 'status': lane['status'],
                                        'metrics': lane_metrics(lane['closed_trips'], since)}
    return out
