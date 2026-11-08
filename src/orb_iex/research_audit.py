"""Read-only operational research; never a strategy backtest or broker writer.

Run: python -m orb_iex.research_audit --root orb_iex
Optional --fetch-quotes retrieves historical SIP quotes for fixed-exit entry-delay
diagnostics. It does not select signals, optimize parameters or place orders.
"""
from __future__ import annotations

import argparse
import gzip
from datetime import datetime, timedelta
from decimal import Decimal as D
import json
import os
from pathlib import Path
import sqlite3


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def first_usable_quote(quotes, target, max_wait=2):
    """Use a fresh quote at/after the delay, never a pre-delay or crossed quote."""
    for q in sorted(quotes, key=lambda q: q['t']):
        age = (instant(q['t']) - target).total_seconds()
        bid, ask = D(str(q['bp'])), D(str(q['ap']))
        if 0 <= age <= max_wait and 0 < bid < ask and q.get('bs', 0) > 0 and q.get('as', 0) > 0:
            return q
    return None


def cost_summary(rows, multiple=1):
    values = [D(r['gross_pnl']) - D(r['modeled_cost']) * multiple for r in rows]
    gains, losses = sum((x for x in values if x > 0), D(0)), -sum((x for x in values if x < 0), D(0))
    wins, loses = [x for x in values if x > 0], [x for x in values if x < 0]
    return {'trades': len(rows), 'net_pnl': str(sum(values, D(0))),
            'expectancy': str(sum(values, D(0))/len(rows)) if rows else None,
            'expectancy_R': str(sum((v/D(r['initial_risk']) for v,r in zip(values,rows)),D(0))/len(rows)) if rows else None,
            'win_rate': len(wins)/len(rows) if rows else None,
            'average_winner': str(sum(wins,D(0))/len(wins)) if wins else None,
            'average_loser': str(sum(loses,D(0))/len(loses)) if loses else None,
            'profit_factor': str(gains/losses) if losses else None,
            'payoff_ratio': str((gains/len(wins))/(losses/len(loses))) if wins and loses else None}


def read_events(log_dir):
    for p in sorted(log_dir.glob('*.jsonl*')):
        opener = gzip.open if p.suffix == '.gz' else open
        with opener(p, 'rt') as stream:
            for line in stream:
                yield json.loads(line)


def broker_exit_fills(orders, symbol, direction, entry_ts, entry_qty):
    """Exit fills for one trade from raw broker orders, across session days (a carried
    position exits on a later day). Used when the local fills table lost the exit."""
    exit_side = 'sell' if direction == 1 else 'buy'
    rows = [o for o in orders if o['symbol'] == symbol and o['side'] == exit_side and D(o['filled_qty']) > 0
            and o['filled_at'] and instant(o['filled_at']) > instant(entry_ts)]
    rows.sort(key=lambda o: o['filled_at'])
    fills, qty = [], D(0)
    for o in rows:
        take = min(D(o['filled_qty']), entry_qty - qty)
        if take <= 0:
            break
        fills.append({'qty': str(take), 'price': o['filled_avg_price'], 'ts': o['filled_at'],
                      'order_id': o['id'], 'role': o['client_order_id'].rsplit('-', 1)[-1]})
        qty += take
    return fills


def broker_reconstruction(snapshot):
    from .reconcile import reconstruct, totals
    closed, opened = reconstruct(snapshot['orders'])
    return {'trades': len(closed), 'open_positions': [(o['symbol'], str(o['open_qty'])) for o in opened],
            'totals': {k: str(v) for k, v in totals(closed).items()},
            'note': 'raw broker fills are the source of truth; compare closed_trades/excluded against this'}


def audit(root):
    db = sqlite3.connect(f'file:{root / "runtime/journal.sqlite"}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    trades = [dict(r) for r in db.execute('SELECT * FROM trades ORDER BY day,symbol')]
    fills = [dict(r) for r in db.execute('SELECT * FROM fills ORDER BY ts,order_id,cum_qty')]
    candidates = {(r['day'],r['symbol']): json.loads(r['details']) for r in db.execute('SELECT * FROM candidates')}
    db.close()
    events = []
    for r in read_events(root/'runtime/logs'):
        # Deliberately exclude huge universe/account/credential diagnostics.
        if r.get('event') in {'signal','order_intent','order_rejected','order_cancel','stop_failed',
                              'stop_update_failed','fill','trade_closed','stop','overnight','orphan_stop'}:
            events.append({k:r[k] for k in ('ts','event','symbol','msg') if k in r})
    snapshot = json.loads((root/'research/broker_snapshot_20261003.json').read_text())
    records, closed, excluded = [], [], []
    for t in trades:
        day, symbol = t['day'], t['symbol']
        fs = [f for f in fills if f['day']==day and f['symbol']==symbol]
        logs = [e for e in events if e['ts'][:10]==day and (e.get('symbol')==symbol or
                (not e.get('symbol') and f'-{symbol}-' in e['msg']))]
        broker = [o for o in snapshot['orders'] if o['symbol']==symbol and
                  o['client_order_id'].startswith('orbiex-'+day.replace('-','')+'-')]
        detail = candidates.get((day,symbol), {})
        record = {'trade':t,'signal':detail,'fills':fs,'events':logs,'broker_orders':broker,
                  'quote_at_stop_submission':None,'quote_missing_reason':'original logs did not persist a usable quote snapshot'}
        records.append(record)
        ins, outs = [f for f in fs if f['role']=='entry'], [f for f in fs if f['role']!='entry']
        iq, oq = sum((D(f['qty']) for f in ins),D(0)), sum((D(f['qty']) for f in outs),D(0))
        source = 'local_fills'
        if iq and iq!=oq:
            # Local state can miss an exit (stale `active` trade) or the exit can land on a later day.
            # The broker's own fills decide; dropping these trades would hide losses from the headline.
            extra = broker_exit_fills(snapshot['orders'], symbol, t['direction'], min(f['ts'] for f in ins), iq)
            if sum((D(f['qty']) for f in extra),D(0)) == iq - oq and not outs:
                per_share = sum((D(f['commission'])+D(f['slippage']) for f in ins),D(0))/iq
                outs = [{**f,'side':'sell' if t['direction']==1 else 'buy','commission':'0',
                         'slippage':str(per_share*D(f['qty']))} for f in extra]
                fs, oq, source = fs+outs, iq, 'broker_reconciled'
        if not iq or iq!=oq:
            excluded.append({'day':day,'symbol':symbol,'entry_qty':str(iq),'exit_qty':str(oq),
                             'reason':'no fills or incomplete same-session round trip; not marked to market'})
            continue
        gross = sum((D(f['qty'])*D(f['price'])*(1 if f['side']=='sell' else -1) for f in fs),D(0))
        cost = sum((D(f['commission'])+D(f['slippage']) for f in fs),D(0))
        # Trade.stop_price is mutable; the candidate retains the original sizing risk.
        risk = abs(D(detail['entry'])-D(detail['stop']))*iq
        if risk<=0: raise ValueError('nonpositive initial risk')
        features = detail.get('features',detail)
        closed.append({'day':day,'symbol':symbol,'direction':t['direction'],'qty':str(iq),
                       'entry_time':ins[0]['ts'],'first_exit_time':outs[0]['ts'],
                       'entry_avg':str(sum((D(f['qty'])*D(f['price']) for f in ins),D(0))/iq),
                       'gross_pnl':str(gross),'modeled_cost':str(cost),'initial_risk':str(risk),
                       'relvol':features.get('relvol'),'atr':t['atr'],'local_state':t['state'],'source':source})
    return {'purpose':'operational fill diagnostic, NOT a strategy backtest; selection and execution are contaminated',
            'broker_snapshot_at':snapshot['observed_at'],'positions':snapshot['positions'],
            'historical_access_probes':snapshot['historical_access_probes'],
            'broker_reconstruction':broker_reconstruction(snapshot),'records':records,'closed_trades':closed,'excluded':excluded,
            'cost_sensitivity':{str(n):cost_summary(closed,n) for n in (1,2,3)},
            'long_short':{side:cost_summary([r for r in closed if r['direction']==direction])
                          for side,direction in [('long',1),('short',-1)]},
            'annualized_metrics':None,'annualized_metrics_reason':'four observed sessions, incomplete trades and defective execution; annualization is not meaningful'}


def quote_stress(root, rows):
    import requests
    from .secrets import load_env_file, load_credentials
    env = dict(os.environ); load_env_file(root/'.env',env)
    headers = load_credentials(env).headers()
    result = []
    for row in rows:
        start = instant(row['entry_time'])
        for seconds in (0,5,30,60):
            target = start+timedelta(seconds=seconds)
            if target >= instant(row['first_exit_time']):
                result.append({'day':row['day'],'symbol':row['symbol'],'delay_seconds':seconds,
                               'unavailable':'observed exit already began before delayed entry'})
                continue
            response = requests.get('https://data.alpaca.markets/v2/stocks/'+row['symbol']+'/quotes',
                headers=headers,params={'start':target.isoformat(),'end':(target+timedelta(seconds=2)).isoformat(),
                'feed':'sip','limit':1000,'sort':'asc'},timeout=20,allow_redirects=False)
            item = {'day':row['day'],'symbol':row['symbol'],'delay_seconds':seconds,'http_status':response.status_code,
                    'target':target.isoformat(),'qty':row['qty'],'gross_pnl_fixed_exit':row['gross_pnl']}
            if response.status_code==200:
                body=response.json(); q=first_usable_quote(body.get('quotes') or [],target)
                item['quote']=q
                if q:
                    px=D(str(q['ap'] if row['direction']==1 else q['bp']))
                    change=D(row['direction'])*D(row['qty'])*(D(row['entry_avg'])-px)
                    item['entry_price']=str(px); item['pnl_change']=str(change)
                    item['net_fixed_exit']=str(D(row['gross_pnl'])-D(row['modeled_cost'])+change)
                else: item['unavailable']='no usable two-sided quote within two seconds'
            else: item['unavailable']='historical quote request failed'
            result.append(item)
    return {'method':'first usable SIP quote within 2s after actual first entry-fill time plus delay; buy ask/sell bid, fixed observed exits and fixed quantity; no queue/depth/borrow simulation',
            'warning':'conditional sensitivity only; cases where exits already began are excluded; historical NBBO size does not guarantee a fill',
            'rows':result}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=Path('orb_iex'))
    p.add_argument('--fetch-quotes',action='store_true');args=p.parse_args()
    report=audit(args.root)
    if args.fetch_quotes: report['delay_stress']=quote_stress(args.root,report['closed_trades'])
    print(json.dumps(report,indent=2))


if __name__=='__main__': main()
