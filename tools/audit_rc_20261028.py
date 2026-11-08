"""Offline frozen-RC audit. Run from orb_iex: .venv314/bin/python tools/audit_rc_20261028.py.
Inputs stay private in runtime/rc-evidence; outputs there are deterministic.
Order snapshots establish aggregate fills, not executable historical liquidity.
"""
import csv
import hashlib
import json
import random
import sqlite3
from collections import defaultdict
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

from orb_iex.reconcile import compare_trips, fills_from_journal, reconstruct
from orb_iex.research_audit import cost_summary

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'runtime/rc-evidence'


def read_rows(name, table):
    with sqlite3.connect(f'file:{EVIDENCE/name}?mode=ro&immutable=1', uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute('SELECT * FROM ' + table)]


def metrics(rows, multiple):
    out = cost_summary(rows, multiple)
    equity = peak = drawdown = D(0)
    for r in sorted(rows, key=lambda r: (r['exit_time'], r['symbol'])):
        equity += D(r['gross_pnl']) - multiple * D(r['modeled_cost'])
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    out['closed_cash_drawdown'] = str(drawdown)
    return out


def bootstrap(rows, multiple):
    days = defaultdict(list)
    for r in rows:
        days[r['day']].append(float(D(r['gross_pnl']) - multiple * D(r['modeled_cost'])))
    blocks = list(days.values())
    rng = random.Random(20261028)
    means = []
    for _ in range(10000):
        sample = [v for block in rng.choices(blocks, k=len(blocks)) for v in block]
        means.append(sum(sample)/len(sample))
    means.sort()
    return [means[249], means[9749]]


def main():
    manifest = json.loads((EVIDENCE/'manifest.json').read_text())
    inputs = {v['snapshot']: v['sha256'] for v in manifest.values() if isinstance(v, dict)}
    inputs['broker-orders.json'] = manifest['broker_orders_sha256']
    for name, expected in inputs.items():
        if hashlib.sha256((EVIDENCE/name).read_bytes()).hexdigest() != expected:
            raise ValueError(f'Frozen evidence changed: {name}')
    orders = json.loads((EVIDENCE/'broker-orders.json').read_text())
    fills = read_rows('journal.sqlite', 'fills')
    candidates = {(r['day'], r['symbol']): r for r in read_rows('journal.sqlite', 'candidates')}
    risks = {k: abs(D(json.loads(r['details'])['entry']) - D(json.loads(r['details'])['stop']))
             for k, r in candidates.items() if 'entry' in json.loads(r['details'])}
    trips, opened = reconstruct(orders, risk_per_share=risks)
    journal, jo = reconstruct(fills=fills_from_journal(fills), risk_per_share=risks)
    issues = compare_trips(trips, journal)
    if issues or opened or jo:
        raise ValueError(f'Reconciliation failed: {issues}; open {len(opened)}/{len(jo)}')
    # Independent signed cash arithmetic, without the reconstruction algorithm.
    cash = defaultdict(lambda: D(0))
    qty = defaultdict(lambda: D(0))
    for o in orders:
        if o['client_order_id'].startswith('orbiex-') and D(o['filled_qty']) > 0:
            q = D(o['filled_qty']) * (1 if o['side'] == 'buy' else -1)
            cash[o['symbol']] -= q * D(o['filled_avg_price'])
            qty[o['symbol']] += q
    for symbol in cash:
        if qty[symbol] or abs(cash[symbol] - sum(t['gross_pnl'] for t in trips if t['symbol'] == symbol)) > D('.000001'):
            raise ValueError(f'Independent cash check failed: {symbol}')
    # Check every filled order, including canceled partials, without pairing trades.
    for o in orders:
        if D(o['filled_qty']) <= 0:
            continue
        fs = [f for f in fills if f['order_id'] == o['id']]
        if sum(D(f['qty']) for f in fs) != D(o['filled_qty']):
            raise ValueError(f'Order quantity differs: {o["id"]}')
        if abs(sum(D(f['qty'])*D(f['price']) for f in fs) - D(o['filled_qty'])*D(o['filled_avg_price'])) > D('.01'):
            raise ValueError(f'Order notional differs: {o["id"]}')
    quotes = {(r['day'], r['symbol']): json.loads(r['detail']) for r in
              read_rows('trade_journal.sqlite', 'exec_events') if r['kind'] == 'entry_quote'}
    ledger = {}
    for line in (ROOT/'docs/loss-journal.jsonl').read_text().splitlines():
        r = json.loads(line)
        if r.get('trade_id'):
            ledger[r['trade_id']] = r
    rows = []
    for t in trips:
        c = candidates.get((t['day'], t['symbol']), {})
        detail = json.loads(c.get('details', '{}'))
        owned = [o for o in orders if o['id'] in t['order_ids'] and D(o['filled_qty']) > 0]
        exits = [o for o in owned if o['side'] == ('sell' if t['direction'] == 1 else 'buy')]
        entries = [o for o in owned if o not in exits]
        old = ledger.get(t['day']+':'+t['symbol'], {})
        q = quotes.get((t['day'],t['symbol']), {})
        trigger = D(detail['entry']) if detail.get('entry') else None
        stop_gap = sum((D(o['filled_qty'])*t['direction']*(D(o['stop_price'])-D(o['filled_avg_price']))
                        for o in exits if o.get('stop_price')), D(0))
        dt = lambda s: datetime.fromisoformat(s.replace('Z','+00:00'))
        row = {k: v for k,v in t.items() if k != 'order_ids'}
        row.update(entry_trigger=trigger, planned_stop=detail.get('stop'), atr=detail.get('atr'),
                   relvol=detail.get('relvol'), adv=detail.get('adv'), candidate_rank=c.get('rank'),
                   entry_adverse_dollars=(t['entry_vwap']-trigger)*t['direction']*t['qty'] if trigger else None,
                   stop_gap_dollars=stop_gap, exit_roles=';'.join(o['client_order_id'].rsplit('-',1)[-1] for o in exits),
                   entry_et=dt(t['entry_time']).astimezone(ZoneInfo('America/New_York')).isoformat(),
                   holding_seconds=(dt(t['exit_time'])-dt(t['entry_time'])).total_seconds(),
                   journal_fill_increments=sum(f['order_id'] in t['order_ids'] for f in fills),
                   partial_entry=any(D(o['filled_qty']) < D(o['qty']) for o in entries),
                   decision_quote_spread=q.get('spread'), decision_quote_age=q.get('quote_age_seconds'),
                   prior_verdict=old.get('verdict'), prior_category=old.get('category'),
                   prior_evidence=old.get('evidence'),
                   causal_limit='Exit role observed; prior verdict not independently proved. No full NBBO/depth path, MAE/MFE or counterfactual.')
        rows.append(row)
    if any(not r['initial_risk'] for r in rows):
        raise ValueError('Missing planned risk')
    summary = {'inputs_sha256': inputs, 'closed_trades': len(rows), 'filled_orders': sum(D(o['filled_qty']) > 0 for o in orders),
               'independent_cash_pnl': str(sum(cash.values())), 'issues': issues,
               'dates': sorted({r['day'] for r in rows}),
               'cost_stress': {str(m): metrics(rows,m) for m in (0,1,2,3)},
               'bootstrap_mean_95_descriptive': {str(m): bootstrap(rows,m) for m in (0,1,2)},
               'by_day': {d: metrics([r for r in rows if r['day']==d],1) for d in sorted({r['day'] for r in rows})},
               'by_direction': {str(d): metrics([r for r in rows if r['direction']==d],1) for d in (-1,1)},
               'without_largest_winner': metrics([r for r in rows if r is not max(rows,key=lambda x:x['gross_pnl'])],1),
               'entry_adverse_dollars': str(sum(r['entry_adverse_dollars'] for r in rows)),
               'stop_gap_dollars': str(sum(r['stop_gap_dollars'] for r in rows)),
               'decision_quote_coverage': sum(r['decision_quote_spread'] is not None for r in rows),
               'carry_trades': sum(r['carry'] for r in rows),
               'note': 'Paper fills. Extra costs are stress deductions, not paid fees; quote at decision is not quote at fill. Nine dates cannot validate regimes.'}
    (EVIDENCE/'reaudit.json').write_text(json.dumps(summary,indent=2,default=str)+'\n')
    with (EVIDENCE/'reaudit-trades.csv').open('w') as f:
        writer = csv.DictWriter(f,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    print(json.dumps({k: summary[k] for k in ('closed_trades','filled_orders','independent_cash_pnl','issues','cost_stress','bootstrap_mean_95_descriptive','entry_adverse_dollars','stop_gap_dollars','decision_quote_coverage','carry_trades')},indent=2))


if __name__ == '__main__':
    main()
