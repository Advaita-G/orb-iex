"""Durable mutation gate. Broker responses acknowledge requests, not completion."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from hashlib import sha256
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .broker import OrderRejected, OrderUncertain
from .orders import TERMINAL, parse_client_order_id
from .safety import SafetyError


class MutationGate:
    def __init__(self, journal, broker, clock):
        self.j, self.broker, self.clock = journal, broker, clock
        self.busy: set[str] = set()
        self.fallback = Path(journal.path + '.emergency.jsonl')
        self.fallback_corrupt = False
        self.journal_failed = self.fallback.exists() and journal.get_meta('emergency_reconciled') != self.fallback_digest()
        if self.journal_failed:
            journal.set_breaker('journal_failure', detail='emergency fallback awaiting reconciliation')
            for line in self.fallback.read_text().splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    self.fallback_corrupt = True
                    journal.set_breaker('journal_failure', detail='incomplete emergency fallback record')
                    continue
                if 'intent_id' in record:
                    journal.save_intent(record)

    def fallback_digest(self):
        return sha256(self.fallback.read_bytes()).hexdigest() if self.fallback.exists() else ''

    def verified_flat(self):
        if self.journal_failed and not self.fallback_corrupt:
            self.j.set_meta('emergency_reconciled', self.fallback_digest())
            self.j.clear_breaker('journal_failure')
            self.journal_failed = False

    def _save(self, intent):
        try:
            self.j.save_intent(intent)
        except sqlite3.Error:
            self.journal_failed = True
            # Emergency mutations retain a write-ahead record even with SQLite unavailable.
            with self.fallback.open('a') as f:
                f.write(json.dumps(intent, default=str) + '\n')
                f.flush()
                os.fsync(f.fileno())
            if intent['role'] == 'entry' and intent['mutation'] == 'submit':
                raise SafetyError('journal failure: new entry blocked')

    def run(self, symbol, role, mutation, req=None, order=None, emergency=False):
        if symbol in self.busy:
            raise OrderUncertain('symbol mutation already in flight')
        if req is not None:
            parsed = parse_client_order_id(req.client_order_id)
            if parsed is None or parsed[1] != symbol or parsed[2] != role:
                raise SafetyError('mutation ownership mismatch')
        if order is not None and parse_client_order_id(order.client_order_id) is None:
            raise SafetyError('foreign order mutation refused')
        if role == 'entry' and mutation == 'submit' and (self.journal_failed or self.j.breakers()):
            raise SafetyError('persistent breaker blocks new entry')
        try:
            unresolved = self.j.intents(True, symbol)
        except sqlite3.Error:
            self.journal_failed = True
            unresolved = []  # emergency caller performs broker reconciliation itself
        if mutation == 'submit' and unresolved and not emergency:
            raise OrderUncertain('unresolved mutation: reconcile before another submit')
        if mutation == 'cancel' and not emergency and any(i['order_id'] == order.order_id and i['mutation'] == 'cancel'
                                         for i in unresolved):
            return None
        if mutation == 'cancel' and unresolved and not emergency:
            return None
        intent = dict(intent_id=uuid4().hex, ts=self.clock.now().isoformat(), symbol=symbol,
                      trade_id=f'{parse_client_order_id(req.client_order_id if req else order.client_order_id)[0]}:{symbol}',
                      role=role, client_order_id=req.client_order_id if req else order.client_order_id,
                      order_id=None if order is None else order.order_id, mutation=mutation,
                      desired_qty=None if req is None else str(req.qty),
                      desired_price=None if req is None or (req.stop_price is None and req.limit_price is None) else str(req.stop_price if req.stop_price is not None else req.limit_price),
                      state='SENDING', response=None, attempt=1)
        self._save(intent)
        self.busy.add(symbol)
        try:
            result = self._send(intent, lambda: self.broker.submit_order(req) if mutation == 'submit' else self.broker.cancel_order(order.order_id))
            intent['state'] = 'ACKED'
            if result is not None:
                intent['order_id'] = result.order_id
                intent['response'] = json.dumps(asdict(result), default=str)
            self._save(intent)
            return result if mutation == 'submit' else True
        except Exception as exc:
            intent['state'] = 'TERMINAL' if isinstance(exc, OrderRejected) and not isinstance(exc, OrderUncertain) else 'AMBIGUOUS'
            intent['response'] = json.dumps({'error': str(exc), 'status': getattr(exc, 'status', None),
                                             'body': getattr(exc, 'body', None),
                                             'classification': getattr(exc, 'classification', 'unknown')}, default=str)
            self._save(intent)
            raise
        finally:
            self.busy.remove(symbol)

    def _send(self, intent, action):
        """Each HTTP retry also has its attempt committed before the write."""
        previous = getattr(self.broker, 'before_write', None)
        first = intent['attempt']
        count = 0
        def before_write():
            nonlocal count
            intent['attempt'] = first + count
            count += 1
            intent['state'] = 'SENDING'
            self._save(intent)
        self.broker.before_write = before_write
        try:
            return action()
        finally:
            self.broker.before_write = previous

    def resolve(self, apply):
        """Never infer a failed submit from absence in an open-order snapshot."""
        unresolved_symbols = set()
        for intent in self.j.intents(True):
            symbol = intent['symbol']
            snap = self.broker.get_order_by_client_id(intent['client_order_id'])
            if snap is None:
                unresolved_symbols.add(symbol)
                self.j.set_breaker('reconciliation_unhealthy', symbol, 'unresolved ' + intent['intent_id'])
                continue
            apply(snap, side_effects=False)
            complete = intent['mutation'] == 'submit' or snap.status in TERMINAL
            if complete:
                intent['state'] = 'TERMINAL'
                intent['order_id'] = snap.order_id
                self._save(intent)
            else:
                unresolved_symbols.add(symbol)
                # Cancellation is idempotent; an ambiguous response can be retried
                # only after an authoritative read confirms the order still exists.
                age = (self.clock.now() - datetime.fromisoformat(intent['ts'])).total_seconds()
                if age >= 5:
                    intent['attempt'] += 1
                    intent['ts'] = self.clock.now().isoformat()
                    self._save(intent)
                    try:
                        self._send(intent, lambda: self.broker.cancel_order(snap.order_id))
                    except (OrderRejected, ConnectionError) as exc:
                        intent['response'] = json.dumps({'error': str(exc), 'status': getattr(exc, 'status', None),
                                                        'body': getattr(exc, 'body', None),
                                                        'classification': getattr(exc, 'classification', 'unknown')}, default=str)
                        self._save(intent)
        for kind, symbol, _, _ in self.j.breakers():
            if kind == 'reconciliation_unhealthy' and symbol and symbol not in unresolved_symbols:
                self.j.clear_breaker(kind, symbol)
