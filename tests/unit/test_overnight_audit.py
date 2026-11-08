"""Additional counterexamples from the independent overnight execution audit."""

import itertools
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from helpers import Harness, unrelated_order
from orb_iex.broker import OrderRejected
from orb_iex.engine import Engine
from orb_iex.orders import OrderRequest, TERMINAL
from tests.unit.test_execution_hardening import live


def held(h):
    h.open_session(); t = h.enter()
    h.sim.last_price['AAA'] = D(50)
    h.sim.fill(t.entry_order_id, D(100), D(50)); h.pump()
    return h.engine.current_stop(h.trade())


def test_wrong_side_stop_is_canceled_immediately_not_after_deadline(tmp_path):
    h = Harness(tmp_path); stop = held(h)
    stop.side = 'buy'; h.j.save_order(stop)
    h.sim.orders[stop.order_id].side = 'buy'
    h.engine.tick(h.clock.now()); h.pump()
    assert h.sim.orders[stop.order_id].status == 'canceled'
    assert all(o.side == 'sell' for o in live(h, 'AAA', 'stop'))


@pytest.mark.parametrize('sizes,side', [([100, 100], 'sell'), ([110], 'sell'), ([100], 'buy'), ([50], 'sell')])
def test_recovered_conflicting_exits_are_canceled_before_remainder_exit(tmp_path, sizes, side):
    h = Harness(tmp_path); stop = held(h)
    manual = unrelated_order(h.sim, 'MANUAL'); h.sim.positions['USER'] = D(12)
    h.sim.cancel_order(stop.order_id); h.drop_events()
    original = []
    for n, qty in enumerate(sizes, 1):
        u = h.sim.submit_order(OrderRequest('AAA', side, D(qty), 'market',
                              f'orbiex-20260924-AAA-flatten{n}'))
        original.append(u.order_id)
    h.drop_events()
    for _ in range(4):
        h.engine.tick(h.clock.now()); h.pump()
    assert all(h.sim.orders[oid].status in TERMINAL for oid in original)
    exits = live(h, 'AAA', 'exit')
    assert len(exits) == 1 and exits[0].qty == D(100) and exits[0].side == 'sell'
    h.sim.fill(exits[0].order_id, D(100), D(50)); h.pump()
    assert h.sim.positions == {'USER': D(12)} and h.sim.orders[manual.order_id].status == 'new'


@pytest.mark.parametrize('permutation', list(itertools.permutations(range(4))))
def test_valid_event_permutations_have_identical_terminal_fill_state(tmp_path, permutation):
    h = Harness(tmp_path); h.open_session(); t = h.enter()
    first = h.sim.get_order_by_client_id(t.entry_client_id)
    events = [replace(first, filled_qty=D(qty), filled_avg_price=D(50) if qty else None,
                      status=status, timestamp=(h.clock.now()+timedelta(seconds=n)).isoformat(),
                      source='stream' if n % 2 else 'rest')
              for n, (qty, status) in enumerate([(0,'new'),(25,'partially_filled'),
                                                (60,'partially_filled'),(100,'filled')])]
    previous = D(0); terminal_seen = False
    for n in permutation:
        h.engine._apply(events[n], side_effects=False)
        h.engine._apply(events[n], side_effects=False)  # duplicate delivery
        order = h.j.order(t.entry_order_id)
        assert order.filled_qty >= previous
        if terminal_seen: assert order.status == 'filled'
        terminal_seen |= order.status == 'filled'; previous = order.filled_qty
    assert h.j.order(t.entry_order_id).filled_qty == D(100)
    assert h.j.owned_qty()['AAA'] == D(100)
    e = Engine(h.j, h.sim, h.settings, h.clock, 'paper'); e.start_day(h.engine.schedule)
    e._apply(events[0], side_effects=False)
    assert h.j.order(t.entry_order_id).status == 'filled' and h.j.owned_qty()['AAA'] == D(100)


@pytest.mark.parametrize('when', ['before_send', 'after_accept'])
def test_process_crash_around_http_retains_intent_and_never_duplicates(tmp_path, when):
    from orb_iex.execution import MutationGate
    from tests.unit.test_ambiguous_broker import FakeAlpaca, make, req
    h = Harness(tmp_path); alpaca = FakeAlpaca(); broker = make(alpaca, tmp_path)
    gate = MutationGate(h.j, broker, h.clock); original = alpaca.request
    def crash(method, url, **kwargs):
        if method == 'POST':
            assert h.j.intents(True)[0]['state'] == 'SENDING'
            if when == 'after_accept': original(method, url, **kwargs)
            raise SystemExit('process disappeared')
        return original(method, url, **kwargs)
    alpaca.request = crash
    with pytest.raises(SystemExit): gate.run('AAA', 'entry', 'submit', req=req('entry'))
    alpaca.request = original
    resumed = MutationGate(h.j, broker, h.clock); adopted = []
    resumed.resolve(lambda u, **kwargs: adopted.append(u))
    assert len(alpaca.posts()) == (1 if when == 'after_accept' else 0)
    if when == 'after_accept':
        assert len(adopted) == 1 and not h.j.intents(True)
    else:
        assert h.j.intents(True) and h.j.breakers() and not adopted


def test_fill_event_queued_before_delayed_post_reply_converges_without_duplicate(tmp_path):
    h = Harness(tmp_path); h.open_session(); original = h.sim.submit_order
    def submit(req):
        snapshot = original(req)
        if req.client_order_id.endswith('-entry'):
            h.clock.advance(3)
            h.sim.fill(snapshot.order_id, D(30), D(50))
        return snapshot  # stale initial REST reply arrives after queued WS fill
    h.sim.submit_order = submit; h.sim.last_price['AAA'] = D(50)
    t = h.enter(); h.pump(); h.engine.tick(h.clock.now()); h.pump()
    assert h.j.owned_qty()['AAA'] == D(30)
    assert len([o for o in h.sim.orders.values() if o.client_order_id.endswith('-entry')]) == 1
    assert h.sim.orders[t.entry_order_id].status == 'canceled'
    assert len(live(h,'AAA','stop')) == 1 and live(h,'AAA','stop')[0].qty == D(30)


@pytest.mark.parametrize('status,message,classification', [
    (403, 'insufficient qty available for order; shares held', 'held_quantity'),
    (422, 'invalid stop_price: sub-penny increment', 'price_increment'),
])
def test_other_rejection_bodies_are_retained_and_recover_flat(tmp_path, status, message, classification):
    h = Harness(tmp_path); h.open_session(); t = h.enter()
    original = h.sim.submit_order; body = {'code': status*10000, 'message': message}
    def reject(req):
        if req.type == 'stop': raise OrderRejected(message, status, body)
        return original(req)
    h.sim.submit_order = reject; h.sim.last_price['AAA'] = D(50); h.sim.auto_fill_market = True
    h.sim.fill(t.entry_order_id, D(100), D(50)); h.pump(); h.engine.tick(h.clock.now()); h.pump()
    intent = next(i for i in h.j.intents() if i['role'] == 'stop')
    error = json.loads(intent['response'])
    assert error['status'] == status and error['body'] == body and error['classification'] == classification
    assert h.sim.list_positions() == {}


def test_pending_replace_stop_fill_is_accounted_before_exit(tmp_path):
    h = Harness(tmp_path); stop = held(h)
    h.sim.orders[stop.order_id].status = 'pending_replace'
    h.sim._emit(h.sim.orders[stop.order_id], 'pending_replace'); h.pump()
    h.sim.fill(stop.order_id, D(30), D('49.80')); h.pump()
    h.engine.tick(h.clock.now()); h.pump()
    exits = live(h, 'AAA', 'exit')
    assert len(exits) == 1 and exits[0].qty == D(70)
    h.sim.fill(exits[0].order_id, D(70), D('49.80')); h.pump()
    assert h.sim.list_positions() == {} and not live(h, 'AAA', 'stop')


def test_incomplete_interrupt_is_a_failed_process_exit(tmp_path):
    from orb_iex.runner import FatalRunnerError
    from tests.unit.test_fatal import runner_for
    h = Harness(tmp_path); h.open_session(); h.enter(); h.sim.cancel_mode = 'pending'
    r = runner_for(h)
    r.step = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(FatalRunnerError) as caught: r.run()
    assert not caught.value.report['complete'] and not caught.value.safety
    assert h.j.breakers() and live(h, 'AAA', 'entry')


def test_normal_paper_return_cancels_entries(tmp_path):
    from tests.unit.test_fatal import runner_for
    h = Harness(tmp_path); h.open_session(); h.enter()
    r = runner_for(h)
    r.run(until=h.clock.now())
    assert not live(h, 'AAA', 'entry')


def test_shutdown_handler_failure_is_not_reported_clean(tmp_path, monkeypatch):
    from orb_iex.runner import FatalRunnerError
    from tests.unit.test_fatal import runner_for
    h = Harness(tmp_path); h.open_session(); h.enter()
    r = runner_for(h)
    r.step = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
    monkeypatch.setattr(r.engine, 'graceful_shutdown', lambda reason: (_ for _ in ()).throw(OSError('disk failed')))
    with pytest.raises(FatalRunnerError): r.run()
    assert not live(h, 'AAA', 'entry')
    assert any(b[0] == 'shutdown_incomplete' for b in h.j.breakers())
    assert h.j.get_meta('fatal_state')


def test_account_lock_excludes_other_journals_but_allows_another_account(tmp_path, monkeypatch):
    from pathlib import Path
    from orb_iex.ops import InstanceLock, InstanceLockError
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    first = InstanceLock.for_account('a'*16).acquire()
    try:
        with pytest.raises(InstanceLockError): InstanceLock.for_account('a'*16).acquire()
        other = InstanceLock.for_account('b'*16).acquire(); other.release()
    finally:
        first.release()
    retry = InstanceLock.for_account('a'*16).acquire(); retry.release()
