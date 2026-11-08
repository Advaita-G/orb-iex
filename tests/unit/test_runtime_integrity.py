from datetime import date
from decimal import Decimal as D

from helpers import Harness
from orb_iex import ops


def safety_exit(h, partial=True):
    h.open_session();t=h.enter()
    h.sim.latest_quote=lambda symbol:{'t':h.clock.now().isoformat(),'bp':'49.50','ap':'49.52'}
    h.sim.fill(t.entry_order_id,D(30 if partial else 100),D(50));h.pump()
    return next(o for o in h.j.orders() if o.role=='flatten' and o.status not in ('canceled','rejected'))


def test_flat_safety_exit_closes_trade_and_survives_reconcile(tmp_path):
    h=Harness(tmp_path);exit_order=safety_exit(h,partial=True)
    h.sim.fill(exit_order.order_id,exit_order.qty,D('49.50'));h.pump()
    assert h.trade().closed and h.trade().state=='closed'
    h.engine.reconcile(h.clock.now())
    assert h.trade().closed and not h.trade().pending_flatten
    assert h.sim.list_positions().get('AAA',D(0))==0


def test_partial_exit_and_unfilled_entry_do_not_become_closed(tmp_path):
    h=Harness(tmp_path);exit_order=safety_exit(h)
    h.sim.fill(exit_order.order_id,D(20),D('49.50'));h.pump()
    assert not h.trade().closed
    h2=Harness(tmp_path/'empty');h2.open_session();h2.enter();h2.engine.reconcile(h2.clock.now())
    assert not h2.trade().closed


def test_crossed_stop_exit_has_specific_quote_evidence(tmp_path):
    h=Harness(tmp_path);safety_exit(h,partial=True)
    detail=h.j.diagnostic(h.day,'protection_exit:AAA')
    assert detail['reason']=='synthetic_stop_breached_during_entry_cancel'
    assert detail['quote']['bp']=='49.50' and detail['error'] is None


def test_unusable_quote_exit_keeps_distinct_error_evidence(tmp_path):
    h=Harness(tmp_path);h.open_session();t=h.enter()
    h.sim.latest_quote=lambda symbol:{'t':h.clock.now().isoformat(),'bp':'49','ap':'52'}
    h.sim.fill(t.entry_order_id,D(30),D(50));h.pump()
    detail=h.j.diagnostic(h.day,'protection_exit:AAA')
    assert detail['reason']=='protection_quote_unusable_during_entry_cancel'
    assert 'wide IEX quote' in detail['error'] and detail['quote']['ap']=='52'


def test_failed_quote_fetch_never_reuses_earlier_quote_evidence(tmp_path):
    h=Harness(tmp_path);h.open_session();t=h.enter()
    h.j.save_diagnostic(h.day,'protection_quote:AAA',{'quote':{'bp':'old'}})
    def unavailable(symbol):raise ConnectionError('network unavailable')
    h.sim.latest_quote=unavailable
    h.sim.fill(t.entry_order_id,D(30),D(50));h.pump()
    detail=h.j.diagnostic(h.day,'protection_exit:AAA')
    assert detail['quote'] is None and 'network unavailable' in detail['error']


def test_heartbeat_detects_stale_flat_trade_record_without_repairing_it(tmp_path):
    h=Harness(tmp_path);exit_order=safety_exit(h)
    h.sim.fill(exit_order.order_id,exit_order.qty,D('49.50'));h.pump()
    trade=h.trade();trade.closed=False;trade.state='protecting';h.j.save_trade(trade)
    ops.write_heartbeat(tmp_path,{'mode':'paper','session':str(h.day),'health':'healthy','health_reasons':[]})
    heartbeat=ops.read_heartbeat(tmp_path)
    assert heartbeat['health']=='degraded'
    assert any('flat_trade_not_closed:AAA' in s for s in heartbeat['health_reasons'])
    assert not h.trade().closed


def test_heartbeat_detects_gapup_coverage_and_recovers_next_session(tmp_path):
    from orb_iex.journal import Journal
    for name in ('gapup_top10','gapup_top5'):
        journal=Journal(tmp_path/(name+'.sqlite'))
        journal.save_diagnostic(date(2026,11,4),'gapup_state',{'decided':True,'inputs_ok':True,
            'entries':{'NXT':{'status':'rejected','reason':'no_valid_quote_in_window'}},'quality':{'wide_quote':4}})
        journal.db.close()
    ops.write_heartbeat(tmp_path,{'mode':'gapup_shadow','session':'2026-11-04','health':'healthy','health_reasons':[]})
    heartbeat=ops.read_heartbeat(tmp_path)
    assert heartbeat['health']=='degraded' and any('quote_coverage:NXT' in x for x in heartbeat['health_reasons'])
    ops.write_heartbeat(tmp_path,{'mode':'gapup_shadow','session':'2026-11-05','health':'healthy','health_reasons':[]})
    assert ops.read_heartbeat(tmp_path)['health']=='healthy'


def test_unreadable_monitor_journal_is_degraded_not_healthy(tmp_path):
    (tmp_path/'journal.sqlite').write_text('corrupt')
    ops.write_heartbeat(tmp_path,{'mode':'paper','session':'2026-11-04','health':'healthy','health_reasons':['existing']})
    heartbeat=ops.read_heartbeat(tmp_path)
    assert heartbeat['health']=='degraded' and 'existing' in heartbeat['health_reasons']
    assert any('integrity_read_failed' in x for x in heartbeat['health_reasons'])


def test_invalid_diagnostic_shape_does_not_crash_trading_heartbeat(tmp_path):
    from orb_iex.journal import Journal
    for name in ('gapup_top10','gapup_top5'):
        journal=Journal(tmp_path/(name+'.sqlite'))
        journal.save_diagnostic(date(2026,11,4),'gapup_state',[])
        journal.db.close()
    ops.write_heartbeat(tmp_path,{'mode':'gapup_shadow','session':'2026-11-04','health':'healthy'})
    assert ops.read_heartbeat(tmp_path)['health']=='degraded'


def test_monitor_uses_runner_actual_journal_not_data_directory_guess(tmp_path):
    from orb_iex.journal import Journal
    journal=Journal(tmp_path/'custom.sqlite');journal.db.close()
    ops.write_heartbeat(tmp_path/'health-root',{'mode':'paper','session':'2026-11-04',
        'journal_path':str(tmp_path/'custom.sqlite'),'health':'healthy'})
    assert ops.read_heartbeat(tmp_path/'health-root')['health']=='healthy'


def test_monitor_preserves_existing_fatal_health(tmp_path):
    (tmp_path/'journal.sqlite').write_text('corrupt')
    ops.write_heartbeat(tmp_path,{'mode':'paper','session':'2026-11-04','health':'fatal'})
    assert ops.read_heartbeat(tmp_path)['health']=='fatal'
