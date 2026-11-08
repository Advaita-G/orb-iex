"""The unattended orchestrator exercises the real engine, with only broker/time fake."""
import json
import sqlite3
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from helpers import Harness, FakeData, bot_leftover, calendar, et, unrelated_order
from orb_iex.broker import OrderUncertain
from orb_iex.http import ApiResponse
from orb_iex.orders import OrderRequest, TERMINAL
from orb_iex.paper_auto import AutoPaperRunner
from orb_iex.runner import FatalRunnerError
from orb_iex.safety import account_fingerprint

DAY = date(2026,10,5)
ACCOUNT = {'id':'auto-fixture','account_number':'PAAUTO','status':'ACTIVE','equity':'100000'}
FP = account_fingerprint(ACCOUNT)


class Controls:
    def __init__(self,h):
        self.h=h;self.account=ACCOUNT.copy();self.down=False;self.open_override=None;self.calls=[]
    def trading_get(self,path,**kwargs):
        self.calls.append(path)
        if self.down:return ApiResponse(503,{}, {},'offline')
        if path=='/v2/account':return ApiResponse(200,self.account,{},'account')
        assert path=='/v2/clock'
        now=self.h.clock.now();session=calendar().today(now)
        opened=bool(session and session.open<=now<session.close)
        if self.open_override is not None:opened=self.open_override
        return ApiResponse(200,{'timestamp':now.isoformat(),'is_open':opened,
                               'next_open':et(DAY,9,30).isoformat(),'next_close':et(DAY,16,0).isoformat()}, {},'clock')


@pytest.fixture(autouse=True)
def owner(monkeypatch):monkeypatch.setenv('ORB_IEX_INSTANCE_ID','auto-test')


def make(tmp_path, start=None, old=False, sim=None, journal=None):
    h=Harness(tmp_path,day=DAY,now=start or et(DAY,8,0),sim=sim,journal=journal)
    h.sim.clock=h.clock.now;h.sim.sink=h.q.put
    if old:
        for sym,qty in [('CTVA',72),('SPHD',409)]:
            bot_leftover(h.j,sym,-qty,day='2026-10-02');h.sim.positions[sym]=-D(qty);h.sim.last_price[sym]=D(20)
    st=replace(h.settings,allowed_account_fingerprints=(FP,),account_owner_instance_id='auto-test')
    client=Controls(h)
    data=FakeData(calendar())
    r=AutoPaperRunner(st,'paper',h.clock,client,data,h.j,broker=h.sim,events=h.q,
        calendar=calendar(),account_source=lambda:ACCOUNT|{'buying_power':'1000000','shorting_enabled':True},
        verified_account={'fingerprint':FP,'instance_id':'auto-test'})
    h.engine=r.engine;r.engine.quote_source=h.default_quotes
    return r,h,client


def at(h,hour,minute=0,second=0):h.clock.advance((et(DAY,hour,minute,second)-h.clock.now()).total_seconds())


def ready(tmp_path):
    r,h,c=make(tmp_path,start=et(DAY,9,35,3))
    r.data.assets=lambda day:[]  # no qualifying symbols; still exercises normal scan/data gates
    r.step();assert r.entry_open
    return r,h,c


def exits(h,sym='CTVA'):
    return [o for o in h.sim.submitted if o.symbol==sym and not o.client_order_id.endswith('-entry')]


def test_start_hours_before_open_reconciles_owned_risk_but_gate_is_closed(tmp_path):
    r,h,_=make(tmp_path,old=True);wait=r.step()
    assert wait==60 and not r.entry_open and h.sim.submitted==[]
    assert h.j.owned_qty()=={'CTVA':D(-72),'SPHD':D(-409)}


def test_closed_market_does_not_spam_flatten_or_busy_loop(tmp_path):
    r,h,c=make(tmp_path,old=True);r.step()
    for _ in range(10):h.clock.advance(60);assert r.step()==60
    assert h.sim.submitted==[] and len(c.calls)<=33


def test_recovery_at_open_flattens_both_then_opens_internal_gate(tmp_path):
    r,h,_=make(tmp_path,old=True);h.sim.auto_fill_market=True;r.step();at(h,9,30)
    r.step();h.pump();r.step()
    assert r.recovery_ready and not r.entry_open and h.j.owned_qty()=={} and h.sim.positions=={}
    r.data.assets=lambda day:[];at(h,9,35,3);r.step();assert r.entry_open
    assert len(exits(h))==1 and len(exits(h,'SPHD'))==1
    assert not h.j.breakers() and not h.j.intents(True)


def test_failed_recovery_keeps_entry_gate_closed_and_persists_blocker(tmp_path):
    r,h,_=make(tmp_path,old=True);r.step();at(h,9,30);r.step()
    h.clock.advance(45);r.step()
    assert not r.entry_open and h.j.breakers()
    assert not any(o.client_order_id.endswith('-entry') for o in h.sim.submitted)


def test_partial_recovery_fill_does_not_duplicate_or_flip(tmp_path):
    r,h,_=make(tmp_path,old=True);r.step();at(h,9,30);r.step()
    o=exits(h)[0];h.sim.fill(next(x.order_id for x in h.sim.orders.values()
        if x.client_order_id==o.client_order_id),D(30),D(20));h.pump();r.step()
    assert h.sim.positions['CTVA']==D(-42) and len(exits(h))==1 and not r.entry_open
    live=next(x for x in h.sim.orders.values() if x.client_order_id==o.client_order_id)
    h.sim.fill(live.order_id,D(42),D(20));h.pump();r.step()
    assert 'CTVA' not in h.sim.positions and len(exits(h))==1


def test_lost_flatten_response_is_adopted_without_duplicate(tmp_path):
    r,h,_=make(tmp_path,old=True);r.step();original=h.sim.submit_order;dropped=set()
    def lost(req):
        out=original(req)
        if req.symbol not in dropped:
            dropped.add(req.symbol);raise OrderUncertain('accepted response lost')
        return out
    h.sim.submit_order=lost;at(h,9,30);r.step();h.pump();r.step()
    assert len(exits(h))==1 and len(exits(h,'SPHD'))==1 and not r.entry_open


def test_duplicate_recovery_update_cannot_double_count(tmp_path):
    r,h,_=make(tmp_path,old=True);r.step();at(h,9,30);r.step()
    h.pump()
    o=next(x for x in h.sim.orders.values() if x.symbol=='CTVA')
    h.sim.fill(o.order_id,D(72),D(20))
    u=h.q.get();h.engine.on_update(u);h.engine.on_update(u);h.pump();r.step()
    assert h.j.owned_qty().get('CTVA',D(0))==0 and len(exits(h))==1


def test_kill_cancels_owned_entry_even_when_broker_is_closed(tmp_path):
    r,h,c=ready(tmp_path);h.enter();c.open_override=False
    Path(h.settings.kill_switch_file).write_text('human brake')
    h.clock.advance(5);r.step();h.pump()
    assert r.kill_latched and not r.entry_open
    assert h.j.orders()[0].status=='canceled'


def test_broker_unavailable_at_open_fails_closed(tmp_path):
    r,h,c=make(tmp_path,old=True);r.step();at(h,9,30);c.down=True
    assert r.step()==60 and not r.entry_open and h.sim.submitted==[]
    assert any(b[0]=='auto_control_failure' for b in h.j.breakers())


def test_external_kill_at_startup_is_never_removed_and_never_allows_entries(tmp_path):
    r,h,_=make(tmp_path,old=True);flag=Path(h.settings.kill_switch_file);flag.write_text('human brake')
    h.sim.auto_fill_market=True;r.step();at(h,9,30);r.step();h.pump()
    assert flag.read_text()=='human brake' and not r.entry_open and r.kill_latched
    assert h.sim.positions=={} and not any(o.client_order_id.endswith('-entry') for o in h.sim.submitted)


def test_external_kill_during_trading_cancels_entry_then_recovers_when_flat(tmp_path):
    r,h,_=ready(tmp_path);h.open_session();t=h.enter()
    flag=Path(h.settings.kill_switch_file);flag.touch();r.step();h.pump()
    assert h.sim.orders[t.entry_order_id].status=='canceled' and not r.entry_open
    flag.unlink();h.clock.advance(5);r.step()
    assert not r.kill_latched and not r.failed  # deletion here is a simulated human action
    assert 'auto_external_kill' not in {b[0] for b in h.j.breakers()}


def test_external_kill_does_not_recover_while_file_present(tmp_path):
    r,h,_=ready(tmp_path);h.open_session();h.enter()
    flag=Path(h.settings.kill_switch_file);flag.touch();r.step();h.pump()
    h.clock.advance(5);r.step()
    # Persistence checks KILL again, which can hide an unsafe recovery in step.
    r._recover_kill()
    assert r.kill_latched is True
    assert 'auto_external_kill' in {b[0] for b in h.j.breakers()}


def test_external_kill_does_not_recover_with_owned_position(tmp_path):
    r,h,_=ready(tmp_path)
    flag=Path(h.settings.kill_switch_file);flag.touch();r.step();h.pump()
    flag.unlink()
    bot_leftover(h.j,'AAA',10,day=DAY.isoformat())
    h.sim.positions['AAA']=D(10);h.sim.last_price['AAA']=D(10)
    h.clock.advance(5);r.step()
    assert r.kill_latched is True
    assert 'auto_external_kill' in {b[0] for b in h.j.breakers()}


def test_external_kill_does_not_recover_with_manual_breaker(tmp_path):
    r,h,_=ready(tmp_path)
    flag=Path(h.settings.kill_switch_file);flag.touch();r.step();h.pump()
    flag.unlink();h.j.set_breaker('manual_review',detail='operator review required')
    h.clock.advance(5);r.step()
    assert r.kill_latched is True
    assert {'auto_external_kill','manual_review'} <= {b[0] for b in h.j.breakers()}


def test_external_kill_does_not_recover_with_invalid_controls(tmp_path):
    r,h,_=ready(tmp_path)
    flag=Path(h.settings.kill_switch_file);flag.touch();r.step();h.pump()
    flag.unlink()
    # Exercise recovery directly: step returns early while controls are invalid.
    # Keep every other recovery prerequisite safe to isolate this guard.
    r.controls_valid=False
    r._recover_kill()
    assert r.kill_latched is True
    assert 'auto_external_kill' in {b[0] for b in h.j.breakers()}
    r.step()
    assert r.kill_latched is True
    assert 'auto_external_kill' in {b[0] for b in h.j.breakers()}


@pytest.mark.parametrize('mismatch',['account','owner'])
def test_changed_identity_refuses_every_entry(tmp_path,monkeypatch,mismatch):
    r,h,c=make(tmp_path)
    if mismatch=='account':c.account['id']='foreign'
    else:monkeypatch.setenv('ORB_IEX_INSTANCE_ID','wrong-owner')
    r.step()
    assert not r.controls_valid and not r.entry_open and h.sim.submitted==[]


def test_foreign_inventory_is_preserved_and_blocks_gate(tmp_path):
    r,h,_=make(tmp_path);h.sim.positions['USER']=D(12);u=unrelated_order(h.sim)
    r.step();at(h,9,30);r.step()
    assert not r.entry_open and h.sim.positions=={'USER':D(12)} and h.sim.orders[u.order_id].status=='new'


def test_unresolved_old_intent_blocks_gate(tmp_path):
    r,h,_=make(tmp_path)
    h.j.save_intent(dict(intent_id='unknown',ts=h.clock.now().isoformat(),symbol='AAA',role='entry',
                        client_order_id='orbiex-20261002-AAA-entry',mutation='submit',state='AMBIGUOUS',attempt=1))
    r.step();at(h,9,30);r.step()
    assert not r.entry_open and h.j.intents(True)


def test_persistent_breaker_blocks_gate(tmp_path):
    r,h,_=make(tmp_path);h.j.set_breaker('manual_review',detail='operator review required')
    r.step();at(h,9,30);r.step()
    assert not r.entry_open and any(b[0]=='manual_review' for b in h.j.breakers())


def test_holiday_waits_for_calendar_session(tmp_path):
    r,h,_=make(tmp_path,start=et(date(2026,9,7),8,0));r.step()
    assert r.schedule.session.day==date(2026,9,8) and not r.entry_open and h.sim.submitted==[]


def test_journal_failure_closes_gate_and_writes_independent_failure_report(tmp_path,monkeypatch):
    r,h,_=ready(tmp_path)
    monkeypatch.setattr(h.j,'orders',lambda *a,**k:(_ for _ in ()).throw(sqlite3.OperationalError('locked')))
    with pytest.raises(FatalRunnerError):r.run(until=h.clock.now()+timedelta(seconds=1))
    assert not r.entry_open
    report=json.loads(r.report_path.read_text());assert report['status']=='FAILED CLOSED' and not report['flat_gate']


def test_recovery_with_orphan_pending_order_cannot_open_gate(tmp_path):
    r,h,_=make(tmp_path);h.sim.cancel_mode='pending'
    h.sim.submit_order(OrderRequest('AAA','sell',D(1),'stop','orbiex-20261002-AAA-stop1',D(10)))
    h.drop_events();r.step();at(h,9,30);r.step()
    assert not r.entry_open and any(o.status not in TERMINAL for o in h.sim.orders.values())


def test_eod_success_records_verified_flat_safe_report(tmp_path):
    r,h,_=ready(tmp_path);h.open_session();t=h.enter();h.sim.last_price['AAA']=D(50)
    h.sim.fill(t.entry_order_id,D(100),D(50));h.pump();h.sim.auto_fill_market=True
    at(h,15,59);r.step();h.pump();r._graceful_shutdown('test_eod')
    report=json.loads(r.report_path.read_text())
    assert report['status']=='SAFE' and report['flat_gate'] and h.sim.positions=={}
    assert report['final_open_orders']==[] and report['unresolved_intents']==[]


def test_eod_failure_persists_breaker_and_never_claims_safe(tmp_path):
    r,h,_=ready(tmp_path);h.open_session();t=h.enter();h.sim.last_price['AAA']=D(50)
    h.sim.fill(t.entry_order_id,D(100),D(50));h.pump();h.sim.cancel_mode='pending'
    at(h,15,59);r.step()
    with pytest.raises(FatalRunnerError):r._graceful_shutdown('test_eod')
    assert any(b[0]=='shutdown_incomplete' for b in h.j.breakers())
    assert json.loads(r.report_path.read_text())['status']=='FAILED CLOSED'


def test_restart_during_recovery_adopts_working_exit_without_duplicate(tmp_path):
    r,h,_=make(tmp_path,old=True);r.step();at(h,9,30);r.step();assert len(exits(h))==1
    r2,h2,_=make(tmp_path,start=h.clock.now(),sim=h.sim,journal=h.j);r2.step()
    assert not r2.entry_open and len(exits(h2))==1 and len(exits(h2,'SPHD'))==1


def test_restart_after_recovery_revalidates_before_first_entry(tmp_path):
    r,h,_=ready(tmp_path)
    r2,h2,_=make(tmp_path,start=h.clock.now(),sim=h.sim,journal=h.j)
    assert not r2.entry_open;r2.step();assert r2.entry_open
    h2.open_session();h2.enter();h2.enter()
    assert len([o for o in h2.sim.submitted if o.client_order_id.endswith('-entry')])==1


def test_actual_normal_scan_is_reused_and_each_coid_submitted_once(tmp_path):
    r,h,_=make(tmp_path);r.engine.quote_source=r.data.entry_quotes
    r.step();at(h,9,35,3);r.step();h.pump();r.step()
    entries=[o for o in h.sim.submitted if o.client_order_id.endswith('-entry')]
    assert len(entries)==20 and len({o.client_order_id for o in entries})==20
    assert h.j.diagnostic(DAY,'corporate_action_snapshot')['complete']


def test_closed_broker_clock_blocks_writes_even_when_calendar_is_open(tmp_path):
    r,h,c=make(tmp_path,old=True);r.step();at(h,9,30);c.open_override=False;r.step()
    assert not r.entry_open and h.sim.submitted==[]
    assert any(b[0]=='auto_market_closed' for b in h.j.breakers())


def test_report_contains_only_fingerprint_suffix_and_human_readable_counts(tmp_path):
    r,h,_=ready(tmp_path);r._persist(force=True)
    raw=r.report_path.read_text();report=json.loads(raw)
    assert FP not in raw and report['account_fingerprint_suffix']==FP[-4:]
    assert set(report['counts'])=={'candidates','entries','fills','stops','exits'}
    assert report['CTVA_SPHD']=={'CTVA':'0','SPHD':'0'}


def test_http_choke_point_rechecks_kill_after_rate_limit_wait(tmp_path):
    from tests.unit.test_ambiguous_broker import FakeAlpaca, make as paper_broker, req
    from orb_iex import constants as C
    from orb_iex.safety import SafetyError
    r,h,_=ready(tmp_path);alpaca=FakeAlpaca();b=paper_broker(alpaca,tmp_path)
    b.mutation_guard=r._mutation_guard
    b._client._limiters[C.PAPER_TRADING_HOST].acquire=lambda:Path(h.settings.kill_switch_file).touch()
    with pytest.raises(SafetyError):b.submit_order(req('entry'))
    assert alpaca.posts()==[] and r.kill_latched and not r.entry_open


@pytest.mark.parametrize('lose_reply',[False,True])
def test_real_http_entry_guard_allows_own_durable_intent_and_adopts_lost_reply(tmp_path,lose_reply):
    from tests.unit.test_ambiguous_broker import FakeAlpaca, make as paper_broker, req
    r,h,_=ready(tmp_path);alpaca=FakeAlpaca(lose=int(lose_reply))
    b=paper_broker(alpaca,tmp_path);b.mutation_guard=r._mutation_guard
    r.broker=r.engine.broker=r.engine.gate.broker=b
    request=req('entry')
    result=r.engine._submit(request,'entry','test genuine HTTP intent path')
    assert result is not None and len(alpaca.posts(request.client_order_id))==1
    assert not h.j.intents(True) and not r.kill_latched


def test_recovery_ready_does_not_allow_entries_before_data_scan(tmp_path):
    r,h,_=make(tmp_path,start=et(DAY,9,31));r.step()
    assert r.recovery_ready and not r.entry_open
    h.open_session();h.enter()
    assert h.sim.submitted==[]


def test_unavailable_action_metadata_persists_failure_not_a_trading_gate(tmp_path):
    r,h,_=make(tmp_path);r.step()
    r.data.corporate_action_snapshot=lambda symbols,day:{'error':'unavailable'}
    at(h,9,35,3);r.step()
    assert not r.entry_open and h.sim.submitted==[]
    assert any(b[0]=='auto_data_failure' for b in h.j.breakers())


def test_wrong_fingerprint_is_masked_in_durable_summary(tmp_path):
    r,h,c=make(tmp_path);c.account['id']='wrong'
    wrong=account_fingerprint(c.account);r.step()
    raw=r.report_path.read_text()
    assert wrong not in raw and FP not in raw and json.loads(raw)['status']=='FAILED CLOSED'


def test_one_launch_runs_full_calendar_lifecycle_and_exits_with_safe_flat_report(tmp_path):
    r,h,_=make(tmp_path,old=True);r.engine.quote_source=r.data.entry_quotes
    h.sim.auto_fill_market=True
    r.run()
    report=json.loads(r.report_path.read_text())
    assert report['status']=='SAFE' and report['flat_gate']
    assert report['counts']['entries']==20 and report['cutoff']['live_entries']==[]
    assert report['eod']=='liquidated' and report['final_positions']=={}
    assert report['CTVA_SPHD']=={'CTVA':'0','SPHD':'0'}
    assert len(exits(h))==1 and len(exits(h,'SPHD'))==1
    assert all(o.status in TERMINAL for o in h.sim.orders.values())


@pytest.mark.parametrize('hour,minute,second', [(9, 29, 59), (15, 59, 59)])
def test_clock_cache_is_refreshed_across_session_boundary(tmp_path, hour, minute, second):
    r, h, c = make(tmp_path, start=et(DAY, hour, minute, second))
    r.step()
    count = len(c.calls)
    h.clock.advance(2)
    r.step()
    assert len(c.calls) > count
    assert not ({'auto_market_closed', 'auto_calendar_failure'} & {b[0] for b in h.j.breakers()})
    if hour == 15:
        assert not r.entry_open and not r._regular_market(h.clock.now())


def test_cached_open_is_unknown_after_early_close(tmp_path):
    r, h, _ = ready(tmp_path)
    r.schedule = replace(r.schedule, session=replace(r.schedule.session, close=et(DAY, 13, 0)))
    r.controls_at, r.controls_valid, r.regular_open = et(DAY, 12, 59, 59), True, True
    assert r._regular_market_state(et(DAY, 13, 0)) == 'unknown'


@pytest.mark.parametrize('hour,minute', [(9, 29), (15, 59)])
def test_clock_reply_crossing_bell_waits_for_post_boundary_confirmation(tmp_path, hour, minute):
    r, h, c = make(tmp_path, start=et(DAY, hour, minute, 59))
    original = c.trading_get
    def delayed(path, **kwargs):
        response = original(path, **kwargs)
        if kwargs.get('purpose') == 'paper_auto:clock':
            h.clock.advance(2)
        return response
    c.trading_get = delayed
    assert r.step() == 1.0
    assert not r.entry_open and h.sim.submitted == []
    assert not ({'auto_market_closed', 'auto_calendar_failure'} & {b[0] for b in h.j.breakers()})
    c.trading_get = original
    r.step()
    assert not ({'auto_market_closed', 'auto_calendar_failure'} & {b[0] for b in h.j.breakers()})
