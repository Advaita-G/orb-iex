"""Supervised recovery and read-only acceptance evidence, without real orders."""
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
import sqlite3

import pytest

from helpers import Harness, et, settings, unrelated_order
from orb_iex.http import ApiResponse
from orb_iex.journal import Journal
from orb_iex.orders import TERMINAL
from orb_iex.paper_state import inspect_paper
from orb_iex.safety import account_fingerprint


@pytest.mark.parametrize('symbol,qty', [('CTVA',72),('SPHD',409)])
def test_weekend_recovery_adopts_unrecorded_friday_fill_and_preserves_foreign(tmp_path,symbol,qty):
    friday=date(2026,10,2);monday=date(2026,10,5)
    h=Harness(tmp_path,day=friday);h.open_session()
    trade=h.enter(symbol,direction=-1,qty=qty,entry='40',stop='41')
    h.sim.last_price[symbol]=D(40)
    h.sim.fill(trade.entry_order_id,D(qty),D(40));h.drop_events()
    assert h.j.owned_qty()=={} and h.j.order(trade.entry_order_id).status=='new'
    manual=unrelated_order(h.sim);h.sim.positions['USER']=D(12);h.drop_events()
    # Mirror Monday REST history: Friday filled order is outside the one-day window.
    original=h.sim.list_orders
    h.sim.list_orders=lambda status,after=None: [o for o in original(status,after)
        if not (after and o.order_id==trade.entry_order_id)]
    from pathlib import Path
    Path(h.settings.kill_switch_file).touch()
    h.sim.auto_fill_market=True
    resumed=Harness(tmp_path,day=monday,now=et(monday,9,31),journal=h.j,sim=h.sim,q=h.q)
    h.sim.clock=resumed.clock.now
    resumed.engine.recover(resumed.clock.now());resumed.pump()
    for _ in range(3):resumed.engine.tick(resumed.clock.now());resumed.pump()
    assert h.j.order(trade.entry_order_id).filled_qty==D(qty)
    assert h.j.owned_qty()=={} and h.sim.list_positions()=={'USER':D(12)}
    assert h.sim.orders[manual.order_id].status=='new'
    assert all(o.status in TERMINAL for o in h.sim.orders.values() if o.symbol==symbol)
    exits=[o for o in h.sim.submitted if o.symbol==symbol and not o.client_order_id.endswith('-entry')]
    assert len(exits)==1 and exits[0].side=='buy' and exits[0].qty==D(qty)
    assert not h.j.intents(True)


ACCOUNT={'id':'fixture','account_number':'PATEST','status':'ACTIVE'}
class ReadsOnly:
    def __init__(self,positions=None,orders=None,status=200):
        self.calls=[];self.status=status
        self.data={'/v2/account':ACCOUNT,'/v2/positions':positions or [],
                   '/v2/orders':orders or [],'/v2/clock':{'is_open':False}}
    def trading_get(self,path,**kwargs):
        self.calls.append(path)
        return ApiResponse(self.status,self.data[path],{},'fixture')


def inspect(tmp_path,client=None,legacy=False,valid=True):
    path=tmp_path/'journal.sqlite'
    if not path.exists():
        j=Journal(path);j.db.close()
        if legacy:
            with sqlite3.connect(path) as db:
                for table in ('intents','breakers','risk'):db.execute(f'DROP TABLE {table}')
    st=replace(settings(tmp_path),allowed_account_fingerprints=(account_fingerprint(ACCOUNT),) if valid else (),
               account_owner_instance_id='test-owner')
    return inspect_paper(client or ReadsOnly(),st,path,instance_id='test-owner')


def test_flat_audit_is_read_only_and_requires_valid_owner(tmp_path):
    assert inspect(tmp_path)['flat_gate']
    assert not inspect(tmp_path,valid=False)['flat_gate']
    before=(tmp_path/'journal.sqlite').read_bytes()
    report=inspect(tmp_path,ReadsOnly(positions=[{'symbol':'MANUAL','qty':'12'}]))
    assert not report['flat_gate'] and report['positions'][0]['symbol']=='MANUAL'
    assert (tmp_path/'journal.sqlite').read_bytes()==before


def test_legacy_absent_tables_are_unknown_not_zero_and_not_migrated(tmp_path):
    report=inspect(tmp_path,legacy=True)
    assert not report['flat_gate'] and not report['schema_ready']
    assert report['breakers'] is None and report['unresolved_intents'] is None
    with sqlite3.connect(tmp_path/'journal.sqlite') as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='intents'").fetchall()


@pytest.mark.parametrize('condition',['network','truncated','breaker','intent','stale_local_entry'])
def test_acceptance_audit_never_claims_clear_when_uncertain(tmp_path,condition):
    inspect(tmp_path)
    client=ReadsOnly()
    if condition=='network':client.status=503
    elif condition=='truncated':client.data['/v2/orders']=[{}]*500
    else:
        with sqlite3.connect(tmp_path/'journal.sqlite') as db:
            if condition=='breaker':db.execute("INSERT INTO breakers(kind,symbol) VALUES('uncertain','CTVA')")
            elif condition=='intent':db.execute("INSERT INTO intents(intent_id,state) VALUES('lost','SENDING')")
            else:db.execute("INSERT INTO orders(order_id,status) VALUES('stale','new')")
    report=inspect(tmp_path,client)
    assert not report['flat_gate']
