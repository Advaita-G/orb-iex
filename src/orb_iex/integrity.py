"""Read-only session checks; these report failures and never repair trading state."""
import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from .orders import TERMINAL


def runtime_integrity(root, mode, session, journal_path=None):
    names = ('gapup_top10', 'gapup_top5') if mode == 'gapup_shadow' else ('journal',)
    result = {'issues': [], 'session': session, 'quote_quality': {}}
    for name in names:
        path = Path(journal_path) if mode == 'paper' and journal_path else Path(root) / (name + '.sqlite')
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.2)) as db:
                db.row_factory = sqlite3.Row
                db.execute('BEGIN')
                for trade in db.execute('SELECT day,symbol FROM trades WHERE closed=0'):
                    fills = list(db.execute('SELECT role,side,qty FROM fills WHERE day=? AND symbol=?', tuple(trade)))
                    entry = sum((Decimal(f['qty']) for f in fills if f['role'] == 'entry'), Decimal(0))
                    net = sum((Decimal(f['qty']) * (1 if f['side']=='buy' else -1) for f in fills), Decimal(0))
                    orders = list(db.execute('SELECT status FROM orders WHERE day=? AND symbol=?', tuple(trade)))
                    if entry and net == 0 and orders and all(o['status'] in TERMINAL for o in orders):
                        result['issues'].append(f'{name}:flat_trade_not_closed:{trade["symbol"]}:{trade["day"]}')
                if mode == 'gapup_shadow':
                    row = db.execute("SELECT body FROM diagnostics WHERE day=? AND kind='gapup_state'", (session,)).fetchone()
                    state = json.loads(row[0]) if row else {}
                    result['quote_quality'][name] = state.get('quality', {})
                    if state.get('decided') and not state.get('inputs_ok'):
                        result['issues'].append(f'{name}:inputs_failed:{state.get("reason")}')
                    missing = [s for s,e in state.get('entries',{}).items() if e.get('reason')=='no_valid_quote_in_window']
                    if missing:
                        result['issues'].append(f'{name}:quote_coverage:{",".join(sorted(missing))}')
                    if state.get('unresolved_open'):
                        result['issues'].append(f'{name}:unresolved_open')
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
            result['issues'].append(f'{name}:integrity_read_failed:{type(exc).__name__}')
    return result
