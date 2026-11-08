from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from orb_iex.marketdata import Quote
from orb_iex.orders import OrderRequest
from orb_iex.shadow_session import ForwardShadowBroker


def quote(now, bid='99.98', ask='100.02'):
    return Quote('AAA', D(bid), D(ask), now, bid_size=100, ask_size=100)


def setup():
    now = [datetime(2026, 10, 29, 13, 36, tzinfo=timezone.utc)]
    events = []
    b = ForwardShadowBroker(events.append, clock=lambda: now[0])
    return b, now, events


def request(side='buy', kind='stop', price=D('100.05'), role='entry'):
    return OrderRequest('AAA', side, D(10), kind, f'orbiex-20261029-AAA-{role}', stop_price=price)


def test_forward_fill_never_uses_a_pre_submission_quote_and_duplicate_is_inert():
    b, now, _ = setup()
    b.submit_order(request())
    assert b.on_quote('AAA', quote(now[0] - timedelta(milliseconds=1), '100.05', '100.06'))
    assert b.list_positions() == {}
    now[0] += timedelta(seconds=1)
    q = quote(now[0], '100.05', '100.06')
    assert b.on_quote('AAA', q)
    assert b.list_positions() == {'AAA': D(10)}
    assert next(iter(b.orders.values())).filled_avg_price == D('100.06')
    assert not b.on_quote('AAA', q)
    assert b.list_positions() == {'AAA': D(10)}


@pytest.mark.parametrize('kind', ['missing', 'stale', 'future', 'nan', 'crossed', 'wide'])
def test_bad_forward_quotes_cannot_fill_or_become_fresh_protection(kind):
    b, now, _ = setup()
    b.submit_order(request())
    q = {'missing': None, 'stale': quote(now[0] - timedelta(seconds=3)),
         'future': quote(now[0] + timedelta(seconds=1)), 'nan': quote(now[0], 'NaN'),
         'crossed': quote(now[0], '101', '100'), 'wide': quote(now[0], '90', '110')}[kind]
    assert not b.on_quote('AAA', q)
    assert b.list_positions() == {}
    with pytest.raises(ConnectionError):
        b.latest_quote('AAA')


def test_market_exit_waits_for_an_observation_and_fills_on_correct_side():
    b, now, _ = setup()
    b.submit_order(request(kind='market', price=None))
    assert b.list_positions() == {}
    b.on_quote('AAA', quote(now[0]))
    assert b.list_positions() == {'AAA': D(10)}
    b.submit_order(request(side='sell', kind='market', price=None, role='eod1'))
    assert b.list_positions() == {'AAA': D(10)}
    now[0] += timedelta(seconds=1)
    b.on_quote('AAA', quote(now[0], '100.10', '100.12'))
    assert b.list_positions() == {}
    assert b.cash_pnl == D('0.80')


def test_short_trigger_uses_bid_and_does_not_invent_a_quote_timestamp():
    b, now, _ = setup()
    b.submit_order(request(side='sell', price=D('99.95')))
    now[0] += timedelta(seconds=1)
    q = quote(now[0], '99.94', '99.96')
    b.on_quote('AAA', q)
    assert b.list_positions() == {'AAA': D(-10)}
    assert b.latest_quote('AAA')['t'] == q.t.isoformat()
