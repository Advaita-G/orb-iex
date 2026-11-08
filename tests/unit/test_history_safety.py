import json
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from helpers import FakeData, SYMBOLS, bar, calendar, et, settings
from orb_iex.history_safety import action_exclusions, session_history_reason, unexplained_discontinuity
from orb_iex.http import ApiResponse
from orb_iex.indicators import atr_sma
from orb_iex.marketdata import BarCache, MarketData
from tests.unit.test_runner import make

DAY=date(2026,10,2)
CASES=json.loads((Path(__file__).parents[1]/'fixtures/history_safety.json').read_text())['cases']


def snapshot(actions=(), day=DAY, symbols=('CTVA',)):
    return {'observed_at':et(day,9,35).isoformat(),'symbols':list(symbols),
            'actions':list(actions),'complete':True}


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['name'])
def test_price_basis_fixtures(case):
    sessions=calendar().sessions_before(DAY,15)
    old=D(case['previous_close']);new=D(case['next_open'])
    daily={s.day:bar(s.open,old,old+1,old-1,old,2_000_000) for s in sessions}
    daily[sessions[-1].day]=bar(sessions[-1].open,new,new+1,new-1,new,2_000_000)
    data=None if case.get('metadata_missing') else snapshot([case['action']] if case.get('action') else [])
    reason=action_exclusions(data,['CTVA'],sessions[0].day,DAY,et(DAY,9,35,3)).get('CTVA')
    reason=reason or unexplained_discontinuity(daily)
    if case['expected']: assert case['expected'] in reason
    else:
        assert reason is None
        assert atr_sma(list(daily.values())) == (D(13)*2+D(16))/14  # economic gap remains in ATR


@pytest.mark.parametrize('missing', ['daily','opening'])
def test_exact_calendar_sessions_cannot_be_backfilled_with_older_bars(missing):
    sessions=calendar().sessions_before(DAY,20)
    daily={s.day:bar(s.open,50,51,49,50,2_000_000) for s in sessions}
    openings={s.day:bar(s.open,50,51,49,50,1000) for s in sessions}
    del (daily if missing=='daily' else openings)[sessions[-3].day]
    assert f'missing_{missing}_session' in session_history_reason(daily,openings,sessions)


def test_holiday_is_not_a_missing_session_and_window_must_match():
    sessions=calendar().sessions_before(date(2026,9,8),15)
    assert date(2026,9,7) not in {s.day for s in sessions}
    daily={s.day:bar(s.open,50,51,49,50,2_000_000) for s in sessions}
    openings={s.day:bar(s.open,50,51,49,50,1000) for s in sessions}
    assert session_history_reason(daily,openings,sessions) is None
    s=sessions[-1];openings[s.day]=bar(s.open+timedelta(minutes=5),50,51,49,50,1000)
    assert 'opening_window_mismatch' in session_history_reason(daily,openings,sessions)


def test_future_effective_action_does_not_change_current_features():
    data=snapshot([{'symbol':'CTVA','kind':'forward_splits','ex_date':'2026-10-05'}])
    assert action_exclusions(data,['CTVA'],date(2026,9,1),DAY,et(DAY,9,35,3))=={}
    data['observed_at']=et(DAY,9,36).isoformat()
    assert 'not_point_in_time' in action_exclusions(data,['CTVA'],date(2026,9,1),DAY,et(DAY,9,35,3))['CTVA']


@pytest.mark.parametrize('action', [{'kind':'spin_offs'}, {'symbol':'CTVA','kind':'spin_offs'},
                                   {'symbol':'CTVA','kind':'spin_offs','ex_date':'bad'}])
def test_ambiguous_action_metadata_excludes_instead_of_guessing(action):
    assert 'ambiguous' in action_exclusions(snapshot([action]),['CTVA'],date(2026,9,1),DAY,et(DAY,9,35,3))['CTVA']


class ActionClient:
    def __init__(self,pages):self.pages=iter(pages);self.calls=[]
    def data_get(self,path,**kwargs):
        self.calls.append((path,kwargs));return next(self.pages)


def response(data,status=200):return ApiResponse(status,data,{},'test response')


def test_action_fetch_paginates_all_process_dates_and_rejects_historical_knowledge(tmp_path):
    client=ActionClient([response({'corporate_actions':{},'next_page_token':'p2'}),
        response({'corporate_actions':{'spin_offs':[{'source_symbol':'CTVA','ex_date':'2026-10-01'}]}})])
    data=MarketData(client,settings(tmp_path),BarCache(tmp_path/'cache'),wall_clock=lambda:et(DAY,9,35))
    result=data.corporate_action_snapshot(['CTVA'],DAY)
    assert result['complete'] and len(result['actions'])==1
    assert client.calls[0][1]['params']['data_quality']=='all'
    assert client.calls[0][1]['params']['end']=='9999-12-31'  # process_date is not ex_date
    assert client.calls[1][1]['params']['page_token']=='p2'
    assert not data.corporate_action_snapshot(['CTVA'],DAY-timedelta(days=1))['complete']
    assert len(client.calls)==2


@pytest.mark.parametrize('pages', [[response({},403)], [response({'corporate_actions':[]})],
    [response({'corporate_actions':{},'next_page_token':'x'}),response({'corporate_actions':{},'next_page_token':'x'})]])
def test_action_fetch_incomplete_or_failed_response_never_authorizes_features(tmp_path,pages):
    data=MarketData(ActionClient(pages),settings(tmp_path),BarCache(tmp_path/'cache'),wall_clock=lambda:et(DAY,9,35))
    result=data.corporate_action_snapshot(['CTVA'],DAY)
    assert not result['complete'] and result['error']
    assert action_exclusions(result,['CTVA'],date(2026,9,1),DAY,et(DAY,9,35,3))


@pytest.mark.parametrize('problem', ['action','daily','opening','metadata'])
def test_production_scan_journals_exclusion_and_never_submits_affected_symbol(tmp_path,problem):
    cal=calendar();sym=SYMBOLS[-1];data=FakeData(cal)
    if problem=='action':
        data.corporate_action_snapshot=lambda symbols,day:snapshot(
            [{'symbol':sym,'kind':'spin_offs','ex_date':day.isoformat()}],day,symbols)
    elif problem=='metadata':
        data.corporate_action_snapshot=lambda symbols,day:{'error':'unavailable'}
    else:
        method='rth_daily_bars' if problem=='daily' else 'opening_bars_history'
        original=getattr(data,method)
        def incomplete(symbols,sessions):
            result=original(symbols,sessions);result[sym].pop(sessions[-2].day);return result
        setattr(data,method,incomplete)
    r,clock,_,j,sim=make(tmp_path,data=data)
    r.run(until=et(date(2026,9,24),9,35,5))
    candidate=next(c for c in j.candidates(date(2026,9,24)) if c['symbol']==sym)
    assert candidate['status']=='filtered' and candidate['reason'].startswith('history:')
    assert sym in j.diagnostic(date(2026,9,24),'history_exclusions')
    assert not any(o.symbol==sym for o in sim.submitted)
    if problem=='metadata':assert sim.submitted==[]


def test_slow_metadata_fetch_cannot_authorize_a_scan_after_deadline(tmp_path):
    r,clock,data,j,sim=make(tmp_path)
    original=data.corporate_action_snapshot
    def slow(symbols,day):
        result=original(symbols,day)
        clock.advance(180)
        return result
    data.corporate_action_snapshot=slow
    r.run(until=et(date(2026,9,24),9,39))
    assert j.day(date(2026,9,24))['scan_status']=='failed'
    assert sim.submitted==[]


def test_merger_actions_use_acquirer_acquiree_symbols():
    merger = {'kind': 'cash_mergers', 'acquiree_symbol': 'CTVA', 'acquirer_symbol': 'ZZZ',
              'effective_date': str(DAY - timedelta(days=3))}
    old = {**merger, 'effective_date': '2020-01-01'}
    out = action_exclusions(snapshot([merger, old]), ['CTVA'], date(2026, 9, 1), DAY, et(DAY, 9, 35, 3))
    assert list(out) == ['CTVA'] and 'corporate_action:cash_mergers' in out['CTVA']
