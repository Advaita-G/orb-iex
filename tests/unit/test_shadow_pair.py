from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
import json
import queue

import pytest

from helpers import Harness, calendar, et, settings, FakeData
from orb_iex.clock import FakeClock
from orb_iex.journal import Journal
from orb_iex.marketdata import Quote, Bar
from orb_iex.orders import OrderRequest
from orb_iex.shadow_session import ForwardShadowBroker, ForwardShadowRunner
from orb_iex.shadow_pair import (SharedObservations, ObservationClock, encode, decode,
                                 atomic_step, restore_broker, session_ledger)

DAY = date(2026, 10, 29)


def broker():
    clock = FakeClock(et(DAY, 9, 36))
    events = []
    b = ForwardShadowBroker(events.append, clock=clock.now)
    return b, clock, events


def quote(clock, size=3, bid='100.05', ask='100.06'):
    return Quote('AAA', D(bid), D(ask), clock.now(), bid_size=size, ask_size=size)


def order(role='entry', side='buy', qty=10, stop='100.05'):
    return OrderRequest('AAA', side, D(qty), 'stop', f'orbiex-20261029-AAA-{role}', stop_price=D(stop))


def test_size_budgets_partial_fills_and_trigger_latches_after_price_recedes():
    b, c, _ = broker()
    o = b.submit_order(order())
    b.on_quote('AAA', quote(c))
    assert b.list_positions() == {'AAA': D(3)}
    assert b.orders[o.order_id].status == 'partially_filled'
    c.advance(1)
    q = quote(c, size=3, bid='100.01', ask='100.02')
    b.on_quote('AAA', q)
    assert b.list_positions() == {'AAA': D(6)}  # stop is now a market order
    assert not b.on_quote('AAA', q)
    assert b.list_positions() == {'AAA': D(6)}
    c.advance(1)
    b.on_quote('AAA', quote(c, size=100))
    assert b.list_positions() == {'AAA': D(10)}


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_one_budget_per_side_not_per_order(side):
    b, c, _ = broker()
    stop = '100.05' if side == 'buy' else '100.06'
    b.submit_order(order(side=side, stop=stop))
    b.submit_order(order(role='eod1', side=side, stop=stop))
    b.on_quote('AAA', quote(c, size=4))
    assert abs(b.list_positions()['AAA']) == 4


@pytest.mark.parametrize('size', [None, 0, -1, True, 1.5, '100'])
def test_missing_or_malformed_size_never_authorizes_fill(size):
    b, c, _ = broker()
    b.submit_order(order())
    b.on_quote('AAA', quote(c, size=size))
    assert not b.list_positions()
    assert b.quote_decisions['AAA'] == 'missing_displayed_size_no_fill'


def test_checkpoint_preserves_partial_order_cash_ids_and_consumed_quote():
    b, c, _ = broker()
    o = b.submit_order(order())
    q = quote(c)
    b.on_quote('AAA', q)
    body = json.loads(json.dumps(b.checkpoint(), default=str))
    recovered = ForwardShadowBroker(lambda _: None, clock=c.now)
    recovered.restore(body)
    assert not recovered.on_quote('AAA', q)
    assert recovered.list_positions() == b.list_positions()
    assert recovered.cash_pnl == b.cash_pnl
    c.advance(1)
    recovered.on_quote('AAA', quote(c, size=7))
    assert recovered.orders[o.order_id].filled_qty == 10
    new = recovered.submit_order(order(role='eod1', side='sell', stop='99'))
    assert new.order_id != o.order_id


def test_codec_roundtrips_history_dates_quotes_decimals_and_bars():
    value = {'AAA': {DAY: Bar(et(DAY, 9, 30), D(1), D(2), D(1), D(2), 100)},
             'quote': Quote('AAA', D(1), D(2), et(DAY, 9, 35), bid_size=3, ask_size=4)}
    assert decode(json.loads(json.dumps(encode(value)))) == value
    with pytest.raises(ValueError):
        decode({'$type': 'os.system', 'fields': {'$map': []}})


def test_shared_quotes_are_immutable_and_reused_on_restart(tmp_path):
    calls = []
    class Data:
        def entry_quotes(self, symbols, now):
            calls.append(symbols)
            return {s: Quote(s, D(1), D(2), now, bid_size=4, ask_size=4) for s in symbols}
    j, clock = Journal(tmp_path / 'observations.sqlite'), ObservationClock()
    tick = {'id': 1, 'at': datetime.now(timezone.utc).isoformat()}
    shared = SharedObservations(Data(), j, clock)
    shared.begin(tick)
    a = shared.entry_quotes(['AAA', 'BBB'], clock.now())
    shared.begin(tick)
    b = shared.entry_quotes(['AAA'], clock.now())
    assert a['AAA'] == b['AAA'] and len(calls) == 1
    new = SharedObservations(Data(), j, ObservationClock())
    new.begin(tick)
    assert new.entry_quotes(['AAA'], clock.now()) == b and len(calls) == 1


def test_opening_bar_retries_are_new_observations_shared_in_the_same_order(tmp_path):
    class Data:
        calls = 0
        def opening_bars(self, symbols):
            self.calls += 1
            return {} if self.calls == 1 else {'AAA': 123}
    data = Data()
    shared = SharedObservations(data, Journal(tmp_path / 'obs.sqlite'), ObservationClock())
    tick = {'id': 1, 'at': datetime.now(timezone.utc).isoformat()}
    for _ in range(2):
        shared.begin(tick)
        assert shared.opening_bars(['AAA']) == {}
        assert shared.opening_bars(['AAA']) == {'AAA': 123}
    assert data.calls == 2


def test_atomic_checkpoint_rolls_back_fill_and_recovers_without_duplicates(tmp_path):
    h = Harness(tmp_path)
    h.open_session()
    events = queue.Queue()
    sim = ForwardShadowBroker(events.put, clock=h.clock.now)
    h.engine.broker = sim
    h.engine.gate.broker = sim
    h.q = events
    h.sim = sim
    h.engine.quote_source = h.default_quotes
    h.enter(qty=10)
    class Step:
        j = h.j
        broker = sim
        clock = h.clock
        fail = True
        def step(self):
            sim.on_quote('AAA', Quote('AAA', D('50.99'), D('51.01'), h.clock.now(), bid_size=3, ask_size=3))
            while not events.empty():
                h.engine.on_update(events.get_nowait())
            if self.fail:
                raise RuntimeError('crash before commit')
            return 5
        def _drain_events(self, now):
            while not events.empty():
                h.engine.on_update(events.get_nowait())
    r = Step()
    initial = json.dumps({'version': 1, 'tick': 0, 'broker': sim.checkpoint()}, default=str)
    h.j.set_meta('forward_checkpoint', initial)
    before = h.j.db.execute('SELECT count(*) FROM fills').fetchone()[0]
    with pytest.raises(RuntimeError):
        atomic_step(r, {'id': 1})
    assert h.j.db.execute('SELECT count(*) FROM fills').fetchone()[0] == before
    restore_broker(h.j, sim)
    r.fail = False
    atomic_step(r, {'id': 1})
    count = h.j.db.execute('SELECT count(*) FROM fills').fetchone()[0]
    assert count > before
    atomic_step(r, {'id': 1})
    assert h.j.db.execute('SELECT count(*) FROM fills').fetchone()[0] == count
    restore_broker(h.j, sim)
    assert h.j.owned_qty() == sim.list_positions()


def test_corrupt_or_uncheckpointed_state_fails_closed(tmp_path):
    h = Harness(tmp_path)
    h.open_session(); h.enter()
    b, _, _ = broker()
    with pytest.raises(ValueError, match='without a checkpoint'):
        restore_broker(h.j, b)
    h.j.set_meta('forward_checkpoint', '{broken')
    with pytest.raises(ValueError):
        restore_broker(h.j, b)


def test_full_session_both_profiles_recovery_accounting_and_review(tmp_path):
    from helpers import SYMBOLS
    from orb_iex.shadow_analytics import paired_summary
    from orb_iex.calendar import MarketCalendar
    cal = calendar()
    wall = FakeClock(et(DAY, 9, 34))
    clock = ObservationClock()
    root = tmp_path / 'candidate_b/paired-v1'
    root.mkdir(parents=True)
    observations = Journal(root / 'observations.sqlite')
    class Data(FakeData):
        def entry_quotes(self, symbols, now):
            out = {}
            for symbol in symbols:
                long = self.symbols.index(symbol) % 2 == 0
                if now < et(DAY, 9, 36):
                    bid, ask, size = ('50.97', '50.98', 1000) if long else ('49.02', '49.03', 1000)
                elif now < et(DAY, 9, 37):
                    bid, ask, size = ('51.01', '51.02', 3) if long else ('48.98', '48.99', 3)
                else:
                    bid, ask, size = ('50.70', '50.71', 1000) if long else ('49.29', '49.30', 1000)
                out[symbol] = Quote(symbol, D(bid), D(ask), now, bid_size=size, ask_size=size)
            return out
    shared = SharedObservations(Data(cal), observations, clock, wall_now=wall.now)
    def make(profile):
        st = settings(root / profile, strategy__research_profile=profile)
        j = Journal(root / f'{profile}.sqlite')
        q = queue.Queue()
        sim = ForwardShadowBroker(q.put, clock=clock.now)
        restore_broker(j, sim)
        return ForwardShadowRunner(st, 'shadow', clock, None, shared, j, broker=sim,
            events=q, calendar=cal, account_source=sim.get_account)
    runners = [make(p) for p in ('baseline_a', 'candidate_b')]
    times = [(9,34,0), (9,35,3), (9,36,0), (9,36,3), (9,37,0),
             (10,30,0), (15,59,0), (15,59,40), (16,0,1), (16,1,1)]
    for idx, (hh, mm, ss) in enumerate(times, 1):
        wall._now = et(DAY, hh, mm, ss)
        tick = {'id': idx, 'at': wall.now().isoformat()}
        for r in runners:
            shared.begin(tick)
            atomic_step(r, tick)
        if idx == 4:
            # Discard both engines and simulators halfway through partial fills.
            for r in runners:
                r.j.db.close()
            runners = [make(p) for p in ('baseline_a', 'candidate_b')]
    session_ledger(observations, runners, cal, wall.now())
    summary = paired_summary(tmp_path)
    assert summary['matched_count'] > 0
    assert summary['portfolios']['candidate_b']['decisions']['day'] == str(DAY)
    assert summary['portfolios']['candidate_b']['decisions']['paired_plans']
    for profile in ('baseline_a', 'candidate_b'):
        p = summary['portfolios'][profile]
        assert p['closed_trips'] and p['positions'] == {} and p['open_trips'] == []
        assert all(t['classification'] == 'ordinary_strategy_loss' for t in p['closed_trips'])
        assert p['totals']['net_modeled_pnl'] < p['totals']['gross_pnl']
        assert any(D(f['qty']) == 3 for f in p['fills'])
        assert not any(t.state == 'shadow_abandoned' for t in runners[0].j.trades(str(DAY)))
    assert summary['qualification']['qualified'] is False
    assert summary['risk_proposal']['active'] is False


def test_long_outage_records_every_exchange_session_once(tmp_path):
    from types import SimpleNamespace
    cal = calendar(end=date(2026, 11, 12))
    observations = Journal(tmp_path / 'obs.sqlite')
    runners = [SimpleNamespace(j=Journal(tmp_path / f'{i}.sqlite'),
        broker=ForwardShadowBroker(lambda _: None), settings=SimpleNamespace(research_profile=str(i))) for i in range(2)]
    now = et(date(2026, 11, 9), 18, 0)
    session_ledger(observations, runners, cal, now)
    first = observations.db.execute("SELECT day,body FROM diagnostics WHERE kind='paired_session' ORDER BY day").fetchall()
    session_ledger(observations, runners, cal, now)
    second = observations.db.execute("SELECT day,body FROM diagnostics WHERE kind='paired_session' ORDER BY day").fetchall()
    assert first == second and len(first) == 8
    assert all(json.loads(row[1])['status'] == 'INCOMPLETE' for row in first)


def test_real_process_death_rolls_back_uncommitted_checkpoint(tmp_path):
    import os, subprocess, sys
    path = tmp_path / 'crash.sqlite'
    j = Journal(path)
    j.set_meta('committed', 'yes')
    code = """
import os,sys
from orb_iex.journal import Journal
j=Journal(sys.argv[1])
with j.transaction():
 j.set_meta('committed','no')
 j.set_meta('forward_checkpoint','partial')
 os._exit(77)
"""
    result = subprocess.run([sys.executable, '-c', code, str(path)],
        env={**os.environ, 'PYTHONPATH': str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'src')})
    assert result.returncode == 77
    assert j.get_meta('committed') == 'yes' and j.get_meta('forward_checkpoint') is None


def test_missing_quotes_are_degraded_health_not_successful_collection(tmp_path):
    h = Harness(tmp_path)
    b = ForwardShadowBroker(lambda _:None, clock=h.clock.now)
    b.on_quote('AAA', None)
    r = ForwardShadowRunner.__new__(ForwardShadowRunner)
    r.__dict__.update(engine=h.engine, stream=None, j=h.j, prep=object(),
        prep_failed_at=None, schedule=h.schedule, broker=b)
    result = ForwardShadowRunner._health(r, h.clock.now())
    assert result['health'] == 'degraded' and 'AAA:missing_quote' in result['health_reasons']
