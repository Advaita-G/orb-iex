"""Read-only analytics from the two authoritative hypothetical state ledgers."""
from datetime import date
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3

from .candidate_review import review
from .reconcile import fills_from_journal, reconstruct, totals


def portfolio(path, day=None):
    path = Path(path)
    if not path.is_file():
        return {'status': 'NOT_COLLECTED', 'closed_trips': [], 'open_trips': [],
                'fills': [], 'decisions': {}, 'totals': None, 'drawdown': None}
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        fills = [dict(r) for r in db.execute('SELECT * FROM fills ORDER BY ts,order_id')]
        breaks = [dict(r) for r in db.execute('SELECT * FROM breakers')]
        orders = [dict(r) for r in db.execute('SELECT * FROM orders ORDER BY created_at,order_id')]
        sessions = [dict(r) for r in db.execute('SELECT day,scan_status,done,eod_status FROM days ORDER BY day')]
        raw = db.execute("SELECT value FROM meta WHERE key='forward_checkpoint'").fetchone()
        checkpoint = json.loads(raw[0]) if raw else None
        last_plan = db.execute("SELECT max(day) FROM diagnostics WHERE kind='ab_plans'").fetchone()[0]
    closed, opened = reconstruct(fills=fills_from_journal(fills))
    if day:
        closed = [t for t in closed if t['day'] == str(day)]
        opened = [t for t in opened if t['day'] == str(day)]
    equity = peak = drawdown = D(0)
    for t in sorted(closed, key=lambda t: (t['exit_time'], t['symbol'])):
        equity += t['net_modeled_pnl']
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        relevant = [b for b in breaks if b['symbol'] in ('', t['symbol'])]
        roles = {f['role'] for f in fills if f['order_id'] in t['order_ids'] and f['role'] != 'entry'}
        if relevant or t['sign_flip'] or t['carry']:
            classification = 'rule_or_execution_failure'
        elif roles and all(r.startswith('stop') and not r.startswith('stopfail') for r in roles):
            classification = 'ordinary_strategy_loss' if t['net_modeled_pnl'] < 0 else 'ordinary_strategy_outcome'
        elif roles and all(r.startswith('eod') for r in roles):
            classification = 'scheduled_exit_loss' if t['net_modeled_pnl'] < 0 else 'scheduled_exit_outcome'
        else:
            classification = 'unknown_process_evidence'
        t['classification'] = classification
        t['fills'] = [f for f in fills if f['order_id'] in t['order_ids']]
        t['modeled_net_2x'] = t['gross_pnl'] - 2 * t['modeled_cost']
    observed = [s for s in sessions if s['scan_status'] is not None]
    latest = last_plan or (observed[-1]['day'] if observed else sessions[-1]['day'] if sessions else None)
    selected = day or (date.fromisoformat(latest) if latest else None)
    decisions = review(path, selected) if selected else {}
    return {'status': 'COLLECTING', 'closed_trips': closed, 'open_trips': opened,
            'totals': totals(closed), 'drawdown': drawdown, 'drawdown_scope': 'closed net trades in exit order; open excursions excluded',
            'fills': [f for f in fills if not day or f['day'] == str(day)],
            'orders': [o for o in orders if not day or o['day'] == str(day)],
            'breakers': breaks, 'sessions': sessions, 'decisions': decisions,
            'checkpoint_tick': checkpoint['tick'] if checkpoint else None,
            'positions': checkpoint['broker']['positions'] if checkpoint else {},
            'hypotheses': decisions.get('hypotheses', []),
            'quote_health': checkpoint['broker'].get('quote_decisions', {}) if checkpoint else {}}


def paired_summary(root, day=None):
    root = Path(root) / 'candidate_b/paired-v1'
    lanes = {p: portfolio(root / f'{p}.sqlite', day) for p in ('baseline_a', 'candidate_b')}
    observations = root / 'observations.sqlite'
    sessions, provenance = [], None
    if observations.is_file():
        with sqlite3.connect(observations.resolve().as_uri() + '?mode=ro', uri=True) as db:
            sessions = [json.loads(r[0]) for r in db.execute(
                "SELECT body FROM diagnostics WHERE kind='paired_session' ORDER BY day")]
            row = db.execute("SELECT value FROM meta WHERE key='provenance'").fetchone()
            provenance = json.loads(row[0]) if row else None
    # Compare only same-session/symbol/direction observed closed trips. An
    # excluded or still-open counterpart is missing, never a zero-return trade.
    def keyed(lane):
        return {(t['day'], t['symbol'], t['direction']): t for t in lane['closed_trips']}
    a, b = (keyed(lanes[p]) for p in ('baseline_a', 'candidate_b'))
    matched = [{'day': k[0], 'symbol': k[1], 'direction': k[2],
                'a_net': a[k]['net_modeled_pnl'], 'b_net': b[k]['net_modeled_pnl'],
                'difference': b[k]['net_modeled_pnl'] - a[k]['net_modeled_pnl']}
               for k in sorted(a.keys() & b.keys())]
    complete = {s['day'] for s in sessions if s['status'] == 'COMPLETE'}
    trades = len([t for t in lanes['candidate_b']['closed_trips'] if t['day'] in complete])
    # day-filtered reports never assert lifetime progress from just one day's
    # trades. This intentionally counts only full, prospectively frozen sessions.
    if day:
        full = portfolio(root / 'candidate_b.sqlite')
        trades = len([t for t in full['closed_trips'] if t['day'] in complete])
    return {'evidence': 'FORWARD_IEX_HYPOTHETICAL_NOT_BROKER_FILLS', 'portfolios': lanes,
            'matched_outcomes': matched, 'matched_count': len(matched),
            'a_only_closed': len(a.keys() - b.keys()), 'b_only_closed': len(b.keys() - a.keys()),
            'sessions': sessions, 'provenance': provenance,
            'qualification': {'complete_sessions': len(complete), 'candidate_trades': trades,
                'required_sessions': 60, 'required_trades': 200,
                'observation_minimum_met': len(complete) >= 60 and trades >= 200,
                'qualified': False, 'automatic_promotion': False,
                'remaining': 'point-in-time/execution evidence, absolute drawdown approval, supervised PAPER acceptance and explicit promotion approval'},
            'comparison_scope': 'independent portfolios on shared recorded observations; simulation is not executable economic proof',
            'risk_proposal': {'active': False, 'proposed_absolute_drawdown_usd': 1000,
                'approval': 'separate risk approval required; current risk settings unchanged'}}
