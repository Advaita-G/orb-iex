"""Observed incidents and adversarial broker/SQLite failures. All offline."""

import ast
import json
import sqlite3
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from helpers import Harness, et, unrelated_order
from orb_iex.broker import OrderRejected, OrderUncertain
from orb_iex.engine import Engine
from orb_iex.journal import Journal
from orb_iex.orders import TERMINAL, role_kind, parse_client_order_id

FIXTURES = json.loads((Path(__file__).parents[1] / 'fixtures/execution_incidents.json').read_text())['incidents']


def live(h, symbol, kind):
    return [o for o in h.sim.orders.values() if o.symbol == symbol and o.status not in TERMINAL
            and role_kind(parse_client_order_id(o.client_order_id)[2]) == kind]


@pytest.mark.parametrize('case', FIXTURES, ids=lambda c: c['symbol'])
def test_observed_incident_replay_converges_without_flip(tmp_path, case):
    h = Harness(tmp_path, day=date.fromisoformat(case['day']))
    h.open_session()
    sym = case['symbol']
    if case.get('overnight'):
        from helpers import bot_leftover
        bot_leftover(h.j, sym, -case['qty'], day=case['day'])
        h.sim.positions[sym] = -D(case['qty'])
        h.sim.positions['MANUAL'] = D(12)
        h.sim.last_price[sym] = D(20)
        h.sim.auto_fill_market = True
        h.clock.advance(3 * 86400)  # October 5: next trading session after Friday October 2
        next_h = Harness(tmp_path, day=h.clock.now().date(), now=h.clock.now(), journal=h.j, sim=h.sim)
        next_h.open_session()
        next_h.engine.recover(h.clock.now()); next_h.pump()
        assert h.sim.list_positions() == {'MANUAL': D(12)}
        assert next_h.j.owned_qty().get(sym, D(0)) == 0
        assert not next_h.j.intents(True)
        assert not live(next_h, sym, 'stop') and not live(next_h, sym, 'exit')
        return
    t = h.enter(sym, direction=case['direction'], qty=case['qty'], entry=case['entry'])
    t.atr = D(case['atr']); h.j.save_trade(t)
    h.sim.cancel_mode = 'pending'
    if sym == 'CBRS':
        h.clock.set(et(h.day, 10, 30)); h.engine.tick(h.clock.now())
        assert h.order(t.entry_order_id).status == 'pending_cancel'
        h.sim.complete_cancels(); h.pump()
        from helpers import bar
        h.sim.on_bar(sym, bar(et(h.day, 15, 59), 177, 178, 176, 177, 10000)); h.pump()
        assert h.sim.list_positions() == {}
        assert h.order(t.entry_order_id).filled_qty == 0
        return
    original = h.sim.submit_order
    h.sim.last_price[sym] = D(case.get('synthetic_quote', case.get('fill_price', case['entry'])))
    def wash_guard(req):
        if parse_client_order_id(req.client_order_id)[2] == 'stop':
            assert not live(h, sym, 'entry'), 'opposite stop submitted before entry cancel confirmed'
        return original(req)
    h.sim.submit_order = wash_guard
    for delta in case['deltas']:
        h.sim.fill(t.entry_order_id, D(delta), D(case.get('fill_price', case['entry'])))
        h.pump()
        if h.order(t.entry_order_id).status not in TERMINAL:
            assert not live(h, sym, 'stop') # wait for remainder confirmation
    h.sim.last_price[sym] = D(case.get('synthetic_quote', case.get('fill_price', case['entry'])))
    h.sim.cancel_mode = 'immediate'
    h.sim.complete_cancels(); h.pump(); h.engine.tick(h.clock.now()); h.pump()
    if sym != 'NKTR':
        assert len(live(h, sym, 'stop')) == 1
        assert live(h, sym, 'stop')[0].qty == sum(case['deltas'])
    else:
        assert not live(h, sym, 'stop')
        assert live(h, sym, 'exit')[0].qty == D(137)
    assert not any(w[0] == 'replace' for w in h.sim.write_log)
    # End each incident with safe cancellation and broker-sized exit.
    h.engine.market_exit(sym, 'flatten', None, 'replay end')
    h.pump(); h.engine.tick(h.clock.now())
    for o in live(h, sym, 'exit'):
        h.sim.fill(o.order_id, o.qty - o.filled_qty, h.sim.last_price[sym]); h.pump()
    h.engine.tick(h.clock.now())
    assert h.sim.list_positions() == {}
    assert not live(h, sym, 'stop') and not live(h, sym, 'exit')
    assert h.j.owned_qty().get(sym, D(0)) == 0


@pytest.mark.parametrize('status', ['accepted', 'pending_new', 'held', 'pending_replace'])
def test_nonworking_stop_not_protected_and_hard_timeout_flattens(tmp_path, status):
    h = Harness(tmp_path); h.open_session(); t = h.enter()
    original = h.sim.submit_order
    def submit(req):
        u = original(req)
        if parse_client_order_id(req.client_order_id)[2] == 'stop':
            h.sim.orders[u.order_id].status = status
            u.status = status
        return u
    h.sim.submit_order = submit
    h.sim.fill(t.entry_order_id, D(100), D(50)); h.pump()
    assert h.trade().protected_qty == 0 and h.trade().state == 'protecting'
    h.clock.advance(h.settings.protection_timeout_seconds + 1)
    h.engine.tick(h.clock.now()); h.pump()
    assert not live(h, 'AAA', 'stop')
    assert len(live(h, 'AAA', 'exit')) == 1
    assert not any(w[0] == 'replace' for w in h.sim.write_log)


def test_stop_fill_beats_cancel_no_extra_exit(tmp_path):
    h = Harness(tmp_path); h.open_session(); t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50)); h.pump()
    stop = h.engine.current_stop(h.trade())
    h.sim.cancel_mode = 'pending'
    h.engine.market_exit('AAA', 'flatten', None, 'test')
    assert not live(h, 'AAA', 'exit')
    h.sim.fill(stop.order_id, D(100), D('49.8')); h.pump()
    assert h.sim.list_positions() == {} and not live(h, 'AAA', 'exit')


def test_pending_cancel_never_allows_racing_exit(tmp_path):
    h = Harness(tmp_path); h.open_session(); t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50)); h.pump()
    h.sim.cancel_mode = 'pending'; h.engine.market_exit('AAA', 'flatten', None, 'test')
    h.clock.advance(120); h.engine.tick(h.clock.now())
    assert not live(h, 'AAA', 'exit')
    assert h.j.breakers()
    assert not h.engine.shutdown()
    assert any(b[0] == 'shutdown_incomplete' for b in h.j.breakers())


def test_partial_exit_timeout_cancel_confirm_then_size_remainder(tmp_path):
    h = Harness(tmp_path); h.open_session(); t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50)); h.pump()
    h.engine.market_exit('AAA', 'flatten', None, 'test')
    first = live(h, 'AAA', 'exit')[0]
    h.sim.fill(first.order_id, D(40), D(50)); h.pump()
    h.clock.advance(h.settings.exit_timeout_seconds + 1); h.engine.tick(h.clock.now()); h.pump()
    h.engine.tick(h.clock.now())
    exits = live(h, 'AAA', 'exit')
    assert len(exits) == 1 and exits[0].qty == D(60)
    assert h.sim.orders[first.order_id].status == 'canceled'


@pytest.mark.parametrize('accepted', [True, False])
def test_lost_submit_response_restart_never_duplicates(tmp_path, accepted):
    h = Harness(tmp_path); h.open_session()
    original = h.sim.submit_order
    def uncertain(req):
        if accepted:
            sink = h.sim.sink
            h.sim.sink = lambda u: None
            original(req)
            h.sim.sink = sink
        raise OrderUncertain('lost response')
    h.sim.submit_order = uncertain
    h.enter(); h.drop_events()
    assert len(h.j.intents()) == 1
    h.sim.submit_order = original
    e = Engine(h.j, h.sim, h.settings, h.clock, 'paper'); e.start_day(h.schedule); e.recover(h.clock.now())
    assert len([r for r in h.sim.submitted if r.client_order_id.endswith('-entry')]) == int(accepted)
    if not accepted:
        assert e.j.breakers() and e.j.intents(True)


def test_lost_cancel_reply_reconciles_before_stop(tmp_path):
    h = Harness(tmp_path); h.open_session(); t = h.enter()
    original = h.sim.cancel_order
    def uncertain(oid):
        original(oid)
        raise OrderUncertain('lost cancel response')
    h.sim.cancel_order = uncertain
    h.sim.fill(t.entry_order_id, D(30), D(50)); h.pump()
    assert len(live(h, 'AAA', 'stop')) == 1
    assert h.order(t.entry_order_id).status == 'canceled'
    assert h.j.intents(True) == []


def test_stale_rest_status_and_quantity_survive_restart(tmp_path):
    h = Harness(tmp_path); h.open_session(); t = h.enter()
    newer = replace(h.order(t.entry_order_id), source='stream', timestamp=h.clock.now().isoformat(),
                    status='pending_cancel', qty=D(100))
    h.engine._apply(newer)
    stale = replace(newer, source='rest', status='new', qty=D(10),
                    timestamp=(h.clock.now()-timedelta(seconds=10)).isoformat())
    j2 = Journal(h.j.path)
    e = Engine(j2, h.sim, h.settings, h.clock, 'paper'); e.start_day(h.schedule); e._apply(stale)
    rec = j2.order(t.entry_order_id)
    assert rec.status == 'pending_cancel' and rec.qty == D(100)
    j2.db.close()


def test_fill_and_order_write_are_atomic(tmp_path, monkeypatch):
    h = Harness(tmp_path); h.open_session(); t = h.enter()
    u = replace(h.order(t.entry_order_id), status='filled', filled_qty=D(100), filled_avg_price=D(50))
    def fail(rec): raise sqlite3.OperationalError('disk failure')
    monkeypatch.setattr(h.j, 'save_order', fail)
    with pytest.raises(sqlite3.Error): h.engine._apply(u)
    assert h.j.fills(h.day.isoformat()) == []
    assert h.j.order(t.entry_order_id).filled_qty == 0


def test_db_locked_entry_never_reaches_broker(tmp_path, monkeypatch):
    h = Harness(tmp_path); h.open_session()
    def fail(intent): raise sqlite3.OperationalError('database locked')
    monkeypatch.setattr(h.j, 'save_intent', fail)
    h.enter()
    assert h.sim.submitted == []
    assert h.engine.gate.fallback.exists()


def test_sqlite_failure_still_allows_owned_emergency_exit(tmp_path, monkeypatch):
    h = Harness(tmp_path); h.open_session(); t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50)); h.pump()
    h.sim.last_price['AAA'] = D(50); h.sim.auto_fill_market = True
    def fail(intent): raise sqlite3.OperationalError('disk failure')
    monkeypatch.setattr(h.j, 'save_intent', fail)
    assert h.engine.emergency_shutdown()
    assert h.sim.list_positions() == {}
    assert h.engine.gate.fallback.exists()
    monkeypatch.undo() # disk/journal repaired before restarting
    e = Engine(h.j, h.sim, h.settings, h.clock, 'paper')
    assert e.gate.journal_failed
    e.start_day(h.schedule); e.recover(h.clock.now())
    assert not e.gate.journal_failed and e.j.intents(True) == []


@pytest.mark.parametrize('bad_quote', [None, 'crossed', 'stale', 'invalid'])
def test_unusable_quote_never_widens_stop(tmp_path, bad_quote):
    h = Harness(tmp_path); h.open_session(); t = h.enter(direction=-1)
    def quote(sym):
        if bad_quote is None: raise ConnectionError('no quote')
        return {'t': (h.clock.now()-timedelta(seconds=60) if bad_quote=='stale' else h.clock.now()).isoformat(),
                'bp': '0' if bad_quote=='invalid' else '51', 'ap': '51'}
    h.sim.latest_quote = quote
    h.sim.fill(t.entry_order_id, D(100), D(50)); h.pump()
    assert not live(h, 'AAA', 'stop')
    assert live(h, 'AAA', 'exit')[0].qty == D(100)


def test_shutdown_cancels_entries_preserves_manual_and_flattens_fill_race(tmp_path):
    h = Harness(tmp_path); h.open_session()
    for s in ('MUB','CG','CRBG','AVDE'): h.enter(s)
    manual = unrelated_order(h.sim)
    h.sim.positions['USER'] = D(7)
    original = h.sim.cancel_order
    def race(oid):
        u = h.sim.orders[oid]
        if u.symbol == 'MUB' and u.filled_qty == 0:
            h.sim.fill(oid, D(30), D(50), emit=False)
        original(oid)
    h.sim.cancel_order = race
    h.sim.last_price['MUB'] = D(50); h.sim.auto_fill_market = True
    assert h.engine.shutdown()
    assert h.sim.positions == {'USER': D(7)}
    assert h.sim.orders[manual.order_id].status == 'new'
    assert not any(o.status not in TERMINAL for o in h.sim.orders.values() if o.symbol != 'MANUAL')


def test_all_engine_mutations_route_through_gate():
    root = Path(__file__).parents[2] / 'src/orb_iex'
    for name in ('engine.py','runner.py'):
        tree = ast.parse((root/name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in ('submit_order','replace_order','cancel_order')


@pytest.mark.parametrize('symbol', ['UMAC', 'IBM', 'NKTR', 'EXTR', 'HOMB', 'LNT', 'NKE'])
def test_stop_submission_rejection_keeps_exact_body_and_recovers(tmp_path, symbol):
    h = Harness(tmp_path); h.open_session(); t = h.enter(symbol)
    original = h.sim.submit_order
    status = 422 if symbol == 'NKTR' else 403
    message = 'stop price must be greater than current price' if symbol == 'NKTR' else 'potential wash trade detected. use complex orders'
    body = {'code': status * 100000, 'message': message,
            'existing_order_id': t.entry_order_id}
    def reject(req):
        if parse_client_order_id(req.client_order_id)[2] == 'stop':
            raise OrderRejected(f'HTTP {status}: ' + body['message'], status, body)
        return original(req)
    h.sim.submit_order = reject
    h.sim.fill(t.entry_order_id, D(40), D(50)); h.pump()
    intents = [i for i in h.j.intents() if i['role']=='stop']
    assert json.loads(intents[0]['response'])['body'] == body
    assert json.loads(intents[0]['response'])['classification'] == ('crossed_stop' if symbol == 'NKTR' else 'wash_trade')
    assert not live(h, symbol, 'stop') and live(h, symbol, 'exit')[0].qty == D(40)


def test_lost_stop_response_does_not_send_exit_beside_adopted_stop(tmp_path):
    h = Harness(tmp_path); h.open_session(); t = h.enter()
    original = h.sim.submit_order
    def uncertain(req):
        result = original(req)
        if parse_client_order_id(req.client_order_id)[2] == 'stop':
            h.drop_events()
            raise OrderUncertain('timeout after accepting stop')
        return result
    h.sim.submit_order = uncertain
    h.sim.fill(t.entry_order_id, D(100), D(50)); h.pump()
    h.engine.tick(h.clock.now()); h.pump()
    assert len(live(h, 'AAA', 'stop')) == 1
    assert not live(h, 'AAA', 'exit')
    assert len([r for r in h.sim.submitted if parse_client_order_id(r.client_order_id)[2]=='stop']) == 1


def test_stale_exit_local_fill_cannot_overexit_or_reverse(tmp_path):
    h = Harness(tmp_path); h.open_session(); t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50)); h.pump()
    h.engine.market_exit('AAA','flatten',None,'test')
    first = live(h,'AAA','exit')[0]
    h.sim.fill(first.order_id,D(40),D(50),emit=False) # broker knows, local journal is stale
    h.engine.market_exit('AAA','flatten',None,'second request')
    assert len(live(h,'AAA','exit')) == 1
    h.sim.fill(first.order_id,D(60),D(50),emit=False)
    h.engine.market_exit('AAA','flatten',None,'third request')
    assert h.sim.list_positions() == {} and not live(h,'AAA','exit')


def test_kod_pending_stop_with_new_fill_uses_cancel_confirm_new(tmp_path):
    h = Harness(tmp_path); h.open_session(); t = h.enter(symbol='KOD',qty=72)
    h.sim.fill(t.entry_order_id,D(34),D(50)); h.pump()
    stop = live(h,'KOD','stop')[0]
    stop.status = 'pending_new'
    h.sim.fill(t.entry_order_id,D(19),D(50)); h.pump()
    assert not any(w[0]=='replace' for w in h.sim.write_log)
    assert stop.status == 'canceled'
    assert live(h,'KOD','stop')[0].qty == D(53)


def test_duplicate_stops_are_confirmed_canceled_before_new_stop(tmp_path):
    h = Harness(tmp_path); h.open_session(); t = h.enter()
    h.sim.fill(t.entry_order_id,D(100),D(50)); h.pump()
    from orb_iex.orders import OrderRequest
    h.sim.submit_order(OrderRequest('AAA','sell',D(100),'stop',
                                   'orbiex-20260924-AAA-stop99',D('49.8')))
    h.drop_events(); h.engine.reconcile(h.clock.now())
    assert len(live(h,'AAA','stop')) == 1
    assert not live(h,'AAA','exit')


def test_shutdown_protected_position_confirms_flat_before_success(tmp_path):
    h = Harness(tmp_path); h.open_session(); t = h.enter()
    h.sim.fill(t.entry_order_id,D(100),D(50)); h.pump()
    h.sim.auto_fill_market = True; h.sim.last_price['AAA'] = D(50)
    assert h.engine.shutdown()
    assert h.sim.list_positions() == {} and h.j.intents(True) == []
    assert h.j.get_meta('shutdown') == 'complete'


def test_python_exception_while_holding_position_runs_shutdown(tmp_path):
    from orb_iex.runner import Runner
    h = Harness(tmp_path); h.open_session(); t = h.enter()
    h.sim.fill(t.entry_order_id,D(100),D(50)); h.pump()
    h.sim.auto_fill_market = True; h.sim.last_price['AAA'] = D(50)
    runner = Runner(h.settings,'paper',h.clock,None,None,h.j,broker=h.sim)
    runner.engine = h.engine
    def fail(): raise RuntimeError('injected strategy exception')
    runner.step = fail
    with pytest.raises(RuntimeError): runner.run()
    assert h.engine._fatal_protect_one('AAA', D(100), h.clock.now()) == 'protected'
    assert len(live(h, 'AAA', 'stop')) == 1


def test_reconnect_adopts_fill_and_protects_before_new_entry(tmp_path):
    from orb_iex.runner import Runner
    h = Harness(tmp_path); h.open_session(); t = h.enter()
    h.sim.fill(t.entry_order_id,D(100),D(50),emit=False)
    runner = Runner(h.settings,'paper',h.clock,None,None,h.j,broker=h.sim,events=h.q)
    runner.engine = h.engine
    h.q.put(('status','connected'))
    runner._drain_events(h.clock.now())
    assert len(live(h,'AAA','stop')) == 1 and h.trade().protected_qty == D(100)


def test_persistent_breaker_blocks_entries_until_reconciliation(tmp_path):
    h = Harness(tmp_path); h.open_session()
    h.j.set_breaker('reconciliation_unhealthy',detail='previous process failed')
    h.enter('NEW')
    assert not h.sim.submitted
    h.engine.reconcile(h.clock.now())
    h.enter('NEXT')
    assert len(h.sim.submitted) == 1


def test_second_paper_process_cannot_run_or_shutdown_first(tmp_path):
    import fcntl
    from orb_iex.runner import Runner
    from orb_iex.safety import SafetyError
    h = Harness(tmp_path)
    runner = Runner(h.settings,'paper',h.clock,None,None,h.j,broker=h.sim)
    with open(h.j.path+'.run.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(SafetyError): runner.run()
    assert h.sim.write_log == []


@pytest.mark.parametrize('direction,breach', [(1, '49.70'), (-1, '50.30')])
def test_synthetic_stop_during_entry_cancel_waits_terminal_before_flatten(tmp_path, direction, breach):
    h = Harness(tmp_path); h.open_session(); t = h.enter(direction=direction)
    h.sim.cancel_mode = 'pending'
    h.sim.fill(t.entry_order_id, D(30), D(50)); h.pump()
    h.sim.last_price['AAA'] = D(breach)
    h.engine.tick(h.clock.now()); h.pump()
    assert h.j.risk('AAA')['exit_role'] == 'flatten'
    assert not live(h, 'AAA', 'stop') and not live(h, 'AAA', 'exit')
    h.sim.cancel_mode = 'immediate'; h.sim.complete_cancels(); h.pump()
    exits = live(h, 'AAA', 'exit')
    assert len(exits) == 1 and exits[0].qty == D(30)
    h.sim.fill(exits[0].order_id, D(30), D(breach)); h.pump()
    assert h.sim.list_positions() == {}


def test_real_sqlite_lock_blocks_entries_and_emergency_flatten_still_works(tmp_path):
    h = Harness(tmp_path); h.open_session(); t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50)); h.pump()
    h.sim.last_price['AAA'] = D(50); h.sim.auto_fill_market = True
    h.j.db.execute('PRAGMA busy_timeout=1')
    other = sqlite3.connect(h.j.path, isolation_level=None)
    other.execute('BEGIN IMMEDIATE')
    try:
        assert h.engine.emergency_shutdown()
        assert h.sim.list_positions() == {}
        assert h.engine.gate.journal_failed and h.engine.gate.fallback.exists()
    finally:
        other.execute('ROLLBACK'); other.close()


def test_each_http_retry_has_a_durable_sending_attempt(tmp_path):
    from tests.unit.test_ambiguous_broker import FakeAlpaca, make, req
    from orb_iex.execution import MutationGate
    h = Harness(tmp_path)
    alpaca = FakeAlpaca(drop=1, how='503')
    broker = make(alpaca, tmp_path)
    original = alpaca.request
    observed = []
    def check(method, url, **kwargs):
        if method == 'POST':
            intent = h.j.intents(True)[0]
            assert intent['state'] == 'SENDING'
            observed.append(intent['attempt'])
        return original(method, url, **kwargs)
    alpaca.request = check
    gate = MutationGate(h.j, broker, h.clock)
    result = gate.run('AAA', 'stop', 'submit', req=req('stop'))
    assert result.status == 'new' and observed == [1, 2]
    assert h.j.intents()[0]['attempt'] == 2


def test_shutdown_cancels_multiple_owned_entries_for_one_symbol(tmp_path):
    from orb_iex.orders import OrderRequest
    h = Harness(tmp_path); h.open_session(); h.enter('AAA')
    h.sim.submit_order(OrderRequest('AAA', 'buy', D(5), 'stop', 'orbiex-20260923-AAA-entry', D(51)))
    h.drop_events()
    report = h.engine.graceful_shutdown('test')
    assert report['complete'] and len(report['entries']) == 2
    assert not live(h, 'AAA', 'entry') and not h.j.intents(True)


def test_corrupt_emergency_fallback_cannot_clear_its_breaker(tmp_path):
    from orb_iex.execution import MutationGate
    h = Harness(tmp_path)
    h.engine.gate.fallback.write_text('{incomplete\n')
    gate = MutationGate(h.j, h.sim, h.clock)
    gate.verified_flat()
    assert gate.journal_failed and gate.fallback_corrupt
    assert any(kind == 'journal_failure' for kind, *_ in h.j.breakers())


def test_extended_hours_limit_price_is_durable_before_submit(tmp_path):
    from orb_iex.orders import OrderRequest
    h = Harness(tmp_path)
    request = OrderRequest('AAA', 'sell', D(5), 'limit',
                           'orbiex-20260924-AAA-postclose1',
                           limit_price=D('49.97'), extended_hours=True)
    original = h.sim.submit_order
    def submit(req):
        intent = h.j.intents(True, 'AAA')[0]
        assert intent['desired_price'] == '49.97' and intent['state'] == 'SENDING'
        return original(req)
    h.sim.submit_order = submit
    h.engine.gate.run('AAA', 'postclose', 'submit', req=request)


def test_fatal_shutdown_does_not_accept_a_wrong_side_stop_as_protection(tmp_path):
    h = Harness(tmp_path); h.open_session(); t = h.enter()
    h.sim.fill(t.entry_order_id, D(100), D(50)); h.pump()
    stop = h.engine.current_stop(h.trade())
    stop.side = 'buy'
    h.j.save_order(stop)
    h.sim.orders[stop.order_id].side = 'buy'
    h.sim.last_price['AAA'] = D(50)
    h.sim.auto_fill_market = True
    action = h.engine._fatal_protect_one('AAA', D(100), h.clock.now())
    h.pump()
    h.engine.tick(h.clock.now()); h.pump()
    assert action == 'flatten_initiated'
    assert h.sim.list_positions() == {} and not live(h, 'AAA', 'stop')


@pytest.mark.parametrize('direction', [1, -1])
def test_wide_quote_during_pending_entry_cancel_cannot_claim_synthetic_protection(tmp_path, direction):
    h = Harness(tmp_path); h.open_session(); t = h.enter(direction=direction)
    h.sim.cancel_mode = 'pending'
    h.sim.latest_quote = lambda sym: {'t': h.clock.now().isoformat(), 'bp': '49', 'ap': '51'}
    h.sim.fill(t.entry_order_id, D(30), D(50)); h.pump()
    assert h.j.risk('AAA')['exit_role'] == 'flatten'
    assert not live(h, 'AAA', 'stop') and not live(h, 'AAA', 'exit')
    h.sim.cancel_mode = 'immediate'; h.sim.complete_cancels(); h.pump()
    exits = live(h, 'AAA', 'exit')
    assert len(exits) == 1 and exits[0].qty == D(30)
    h.sim.fill(exits[0].order_id, D(30), D(50)); h.pump()
    assert h.sim.list_positions() == {}
