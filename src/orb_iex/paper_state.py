"""Read-only acceptance snapshot. Never migrates the journal or arms a broker.

The flat gate is deliberately account-wide: foreign inventory requires operator
review, never liquidation by this inspector. This is evidence, not reconciliation.
"""
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .orders import TERMINAL
from .safety import check_account_allowlist, check_instance_owner, check_paper_account, SafetyError


def inspect_paper(client, settings, journal_path, instance_id=None):
    report = {'observed_at': datetime.now(timezone.utc).isoformat(),
              'paper_verified': False, 'ownership_valid': False, 'flat_gate': False,
              'errors': [], 'positions': None, 'open_orders': None,
              'journal_owned_qty': None, 'breakers': None, 'unresolved_intents': None,
              'journal_nonterminal_orders': None, 'schema_ready': False}
    def get(path, params=None):
        response = client.trading_get(path, params=params, purpose='paper_state')
        if not response.ok:
            raise ConnectionError(f'{path}: HTTP {response.status}')
        return response.data
    try:
        account = get('/v2/account')
        check_paper_account(account, settings.expected_account_prefix)
        report['paper_verified'] = True
        try:
            check_account_allowlist(account, settings.allowed_account_fingerprints)
            check_instance_owner(settings.account_owner_instance_id,
                                 instance_id if instance_id is not None else os.getenv('ORB_IEX_INSTANCE_ID'))
            report['ownership_valid'] = True
        except SafetyError:
            report['errors'].append('account allowlist or owner instance is invalid; run account-fingerprint and verify configuration')
        positions = get('/v2/positions')
        orders = get('/v2/orders', {'status': 'open', 'limit': 500, 'nested': 'false'})
        if not isinstance(positions, list) or not isinstance(orders, list) or len(orders) >= 500:
            raise ValueError('invalid or potentially truncated broker snapshot')
        report['positions'] = [{k: p[k] for k in ('symbol', 'qty', 'qty_available') if k in p}
                               for p in positions]
        report['open_orders'] = [{k: o.get(k) for k in ('id', 'client_order_id', 'symbol', 'side',
                                 'type', 'status', 'qty', 'filled_qty')} for o in orders]
        clock = get('/v2/clock')
        report['clock'] = {k: clock.get(k) for k in ('timestamp', 'is_open', 'next_open', 'next_close')}
    except Exception as exc:
        report['errors'].append(f'broker snapshot unavailable: {type(exc).__name__}')
    try:
        uri = Path(journal_path).resolve().as_uri() + '?mode=ro'
        with sqlite3.connect(uri, uri=True, timeout=2) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')  # coherent read-only journal snapshot
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            report['schema_ready'] = {'breakers', 'intents', 'risk'} <= tables
            if not report['schema_ready']:
                report['errors'].append('legacy journal: breaker/intent/risk state is unknown until tested startup migration')
            if 'breakers' in tables:
                report['breakers'] = [dict(r) for r in db.execute('SELECT kind,symbol,detail,ts FROM breakers')]
            if 'intents' in tables:
                report['unresolved_intents'] = [dict(r) for r in db.execute(
                    "SELECT intent_id,symbol,role,state,client_order_id FROM intents WHERE state != 'TERMINAL'")]
            owned = {}
            for r in db.execute('SELECT symbol,side,qty FROM fills'):
                owned[r['symbol']] = owned.get(r['symbol'], Decimal(0)) + Decimal(r['qty']) * (1 if r['side']=='buy' else -1)
            for r in db.execute('SELECT symbol,qty FROM owned_adjust'):
                owned[r['symbol']] = owned.get(r['symbol'], Decimal(0)) + Decimal(r['qty'])
            report['journal_owned_qty'] = {s: str(q) for s,q in owned.items() if q}
            report['journal_nonterminal_orders'] = [dict(r) for r in db.execute(
                'SELECT symbol,client_order_id,status,filled_qty FROM orders') if r['status'] not in TERMINAL]
    except Exception as exc:
        report['errors'].append(f'journal snapshot unavailable: {type(exc).__name__}')
    report['flat_gate'] = (not report['errors'] and report['schema_ready'] and report['ownership_valid']
        and all(report[k] is not None and not report[k] for k in
                ('positions', 'open_orders', 'journal_owned_qty', 'journal_nonterminal_orders',
                 'breakers', 'unresolved_intents')))
    return report
