"""Forward-only quote simulation using the existing engine; no broker writes.

Prices are observed IEX best bid/ask, not consolidated executable fills. Queue,
depth, latency and market impact remain unknown. This does not qualify B.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import itertools
from decimal import Decimal

from .orders import TERMINAL, OrderUpdate
from .runner import Runner
from .sim import SimBroker


class ForwardShadowBroker(SimBroker):
    def __init__(self, *args, max_age=2, max_spread=Decimal('1'), **kwargs):
        super().__init__(*args, auto_fill_market=False, **kwargs)
        self.max_age = max_age
        self.max_spread = max_spread
        self.quotes = {}
        self.quote_decisions = {}
        self.triggered = set()

    def on_quote(self, symbol, q):
        now = self.clock()
        reason = None
        if q is None:
            reason = 'missing_quote'
        elif q.symbol != symbol or q.feed != 'iex':
            reason = 'wrong_symbol_or_feed'
        elif q.t is None or q.t.tzinfo is None:
            reason = 'missing_quote_timestamp'
        elif (now - q.t).total_seconds() < 0:
            reason = 'future_quote'
        elif (now - q.t).total_seconds() > self.max_age:
            reason = 'stale_quote'
        elif q.bid is None or q.ask is None or not all(x.is_finite() and x > 0 for x in (q.bid, q.ask)):
            reason = 'invalid_or_one_sided_quote'
        elif q.ask <= q.bid:
            reason = 'locked_or_crossed_quote'
        elif (q.ask - q.bid) / ((q.ask + q.bid) / 2) * 100 > self.max_spread:
            reason = 'wide_quote'
        if reason:
            self.quote_decisions[symbol] = reason
            return False
        previous = self.quotes.get(symbol)
        if previous and q.t <= previous.t:
            self.quote_decisions[symbol] = 'duplicate_or_out_of_order_quote'
            return False
        self.quotes[symbol] = q
        self.last_price[symbol] = (q.bid + q.ask) / 2
        # Reported round-lot counts are only a one-share-per-unit lower bound.
        # Each side's budget is shared by ALL orders in this portfolio, once.
        def size(value):
            return (Decimal(value) if type(value) is int and value > 0 else Decimal(0))
        budget = {'buy': size(q.ask_size), 'sell': size(q.bid_size)}
        self.quote_decisions[symbol] = ('observed_size' if all(budget.values())
                                        else 'missing_displayed_size_no_fill')
        for order in list(self.orders.values()):
            if (order.symbol != symbol or order.status in TERMINAL
                    or order.status == 'pending_cancel'
                    or q.t < self.created[order.order_id]):
                continue
            price = q.ask if order.side == 'buy' else q.bid
            trigger = (order.order_id in self.triggered or order.type == 'market'
                       or (order.type == 'stop' and order.stop_price is not None
                           and (price >= order.stop_price if order.side == 'buy'
                                else price <= order.stop_price))
                       or (order.type == 'limit' and order.order_id in self.limit_prices
                           and (price <= self.limit_prices[order.order_id] if order.side == 'buy'
                                else price >= self.limit_prices[order.order_id])))
            if trigger:
                if order.type == 'stop':
                    self.triggered.add(order.order_id)
                qty = min(order.qty - order.filled_qty, budget[order.side])
                if qty > 0:
                    self.fill(order.order_id, qty, price)
                    budget[order.side] -= qty
        return True

    def checkpoint(self):
        return {'version': 1, 'equity': str(self.equity), 'cash': str(self.cash_pnl),
                'orders': [asdict(o) for o in self.orders.values()],
                'created': {k: v.isoformat() for k, v in self.created.items()},
                'positions': self.positions, 'last_price': self.last_price,
                'limits': self.limit_prices,
                'quotes': {k: asdict(q) for k, q in self.quotes.items()},
                'quote_decisions': self.quote_decisions, 'triggered': sorted(self.triggered)}

    def restore(self, body):
        from .marketdata import Quote
        if body.get('version') != 1:
            raise ValueError('unsupported SHADOW checkpoint version')
        def money(value):
            result = Decimal(str(value))
            if not result.is_finite():
                raise ValueError('nonfinite SHADOW checkpoint value')
            return result
        self.equity, self.cash_pnl = money(body['equity']), money(body['cash'])
        self.orders = {}
        for row in body['orders']:
            row = dict(row)
            for key in ('qty', 'filled_qty', 'filled_avg_price', 'stop_price'):
                if row.get(key) is not None:
                    row[key] = money(row[key])
            o = OrderUpdate(**row)
            if o.qty <= 0 or not 0 <= o.filled_qty <= o.qty or o.side not in ('buy', 'sell'):
                raise ValueError('invalid SHADOW order checkpoint')
            if o.order_id in self.orders:
                raise ValueError('duplicate SHADOW order checkpoint')
            self.orders[o.order_id] = o
        self.created = {k: datetime.fromisoformat(v) for k, v in body['created'].items()}
        if set(self.created) != set(self.orders) or any(t.tzinfo is None for t in self.created.values()):
            raise ValueError('invalid SHADOW creation timestamps')
        self.positions = {k: money(v) for k, v in body['positions'].items()}
        self.last_price = {k: money(v) for k, v in body['last_price'].items()}
        self.limit_prices = {k: money(v) for k, v in body['limits'].items()}
        self.quotes = {}
        for symbol, row in body['quotes'].items():
            row = dict(row)
            for key in ('bid', 'ask'):
                if row.get(key) is not None:
                    row[key] = money(row[key])
            row['t'] = datetime.fromisoformat(row['t'])
            if row['t'].tzinfo is None:
                raise ValueError('naive SHADOW quote timestamp')
            self.quotes[symbol] = Quote(**row)
        self.quote_decisions = body.get('quote_decisions', {})
        self.triggered = set(body.get('triggered', []))
        if not self.triggered <= self.orders.keys():
            raise ValueError('invalid SHADOW triggered order checkpoint')
        self._ids = itertools.count(1 + max([int(k.removeprefix('sim-')) for k in self.orders] or [0]))

    def latest_quote(self, symbol):
        q = self.quotes.get(symbol)
        if q is None:
            raise ConnectionError('no observed forward IEX quote')
        return {'t': q.t.isoformat(), 'bp': str(q.bid), 'ap': str(q.ask)}


class ForwardShadowRunner(Runner):
    """Keep strategy/engine untouched; replace only the bar-based simulator feed."""
    def _abandon_shadow_state(self):
        # The paired collector restores the durable simulator before recovery.
        if self.j.get_meta('forward_checkpoint') is None and self.j.orders():
            raise RuntimeError('legacy SHADOW orders lack a recoverable checkpoint; preserve for review')

    def _health(self, now):
        health = super()._health(now)
        reasons = health['health_reasons']
        if self.prep is None and self.prep_failed_at is not None:
            reasons.append('market_history_unavailable')
        if self.schedule:
            status = self.j.day(self.schedule.session.day)['scan_status']
            if status in ('failed', 'missed'):
                reasons.append('scan_' + status)
        reasons.extend(f'{symbol}:{reason}' for symbol, reason in self.broker.quote_decisions.items()
                       if reason not in ('observed_size', 'duplicate_or_out_of_order_quote'))
        return {'health': 'degraded' if reasons else 'healthy', 'health_reasons': reasons}

    def _shadow_feed(self, now):
        symbols = self._feed_symbols()
        if not symbols or (self.last_feed and (now - self.last_feed).total_seconds() < 2):
            return
        self.last_feed = now
        quotes = self.data.entry_quotes(symbols, now)
        # A slow response or recovered pending tick must not fill an entry after
        # its cutoff. This affects this local simulator only.
        self.engine.enforce_entry_cutoff(self.clock.now())
        self._drain_events(self.clock.now())
        for symbol in symbols:
            q = quotes.get(symbol)
            received = self.clock.now().isoformat()
            body = {**(asdict(q) if q else {'symbol': symbol, 'unavailable': True}),
                    'received_at': received,
                    'evidence': 'IEX quote; hypothetical fills; no queue/depth proof'}
            self.j.save_diagnostic(self.schedule.session.day,
                                   f'forward_quote:{symbol}:{q.t.isoformat() if q and q.t else received}', body)
            if self.broker.on_quote(symbol, q):
                self.last_data_update = self.clock.now().isoformat()
                self._drain_events(self.clock.now())
            reason = self.broker.quote_decisions[symbol]
            if reason not in ('observed_size', 'duplicate_or_out_of_order_quote'):
                self.j.save_diagnostic(self.schedule.session.day, 'forward_gap:' + symbol,
                                       {'reason': reason, 'observed_at': received})


def main():
    from .shadow_pair import main as paired_main
    return paired_main()



if __name__ == '__main__':
    raise SystemExit(main())
