from datetime import datetime, timezone
from decimal import Decimal as D

from orb_iex.research_audit import cost_summary, first_usable_quote, read_events


def test_delay_quotes_require_post_delay_fresh_uncrossed_two_sided_market():
    target=datetime(2026,10,2,13,35,tzinfo=timezone.utc)
    def q(ts,bid=50,ask=51,bs=10):
        return {'t':ts,'bp':bid,'ap':ask,'bs':bs,'as':10}
    before=q('2026-10-02T13:34:59Z')
    crossed=q('2026-10-02T13:35:00Z',52,51)
    no_size=q('2026-10-02T13:35:00.2Z',bs=0)
    good=q('2026-10-02T13:35:01Z')
    stale=q('2026-10-02T13:35:03Z')
    assert first_usable_quote([stale,before,crossed,no_size,good],target)==good
    assert first_usable_quote([before,crossed,no_size,stale],target) is None


def test_cost_stress_uses_all_sides_and_preserves_losses_and_risk_units():
    rows=[{'gross_pnl':'10','modeled_cost':'2','initial_risk':'5'},
          {'gross_pnl':'-5','modeled_cost':'1','initial_risk':'10'}]
    one=cost_summary(rows,1);three=cost_summary(rows,3)
    assert D(one['net_pnl'])==2 and D(three['net_pnl'])==-4
    assert D(one['expectancy_R'])==D('.5') and one['win_rate']==.5
    assert D(one['profit_factor'])==D(8)/6
    assert cost_summary([])['expectancy'] is None


def test_rotated_logs_remain_part_of_historical_evidence(tmp_path):
    import gzip
    (tmp_path/'today.jsonl').write_text('{"symbol":"AAA"}\n')
    with gzip.open(tmp_path/'yesterday.jsonl.gz','wt') as f:
        f.write('{"symbol":"BBB"}\n')
    assert {r['symbol'] for r in read_events(tmp_path)}=={'AAA','BBB'}


def test_report_risk_uses_original_candidate_not_mutated_stop(tmp_path):
    import json
    import sqlite3
    from orb_iex.research_audit import audit
    (tmp_path/'runtime').mkdir();(tmp_path/'research').mkdir()
    c=sqlite3.connect(tmp_path/'runtime/journal.sqlite')
    c.executescript("""
      CREATE TABLE trades(day,symbol,direction,entry_price,stop_price,atr,state);
      INSERT INTO trades VALUES('2026-10-02','AAA',1,'50','49.99','10','stopped_out');
      CREATE TABLE fills(day,symbol,role,side,qty,price,ts,commission,slippage,order_id,cum_qty);
      INSERT INTO fills VALUES('2026-10-02','AAA','entry','buy','10','50','2026-10-02T13:35:00Z','0','0','one','10');
      INSERT INTO fills VALUES('2026-10-02','AAA','stop','sell','10','49','2026-10-02T13:36:00Z','0','0','two','10');
      CREATE TABLE candidates(day,symbol,details);
    """)
    c.execute('INSERT INTO candidates VALUES(?,?,?)',('2026-10-02','AAA',json.dumps({'entry':'50','stop':'49'})))
    c.commit();c.close()
    (tmp_path/'research/broker_snapshot_20261003.json').write_text(json.dumps(
        {'observed_at':'test','positions':{},'orders':[],'historical_access_probes':[]}))
    result=audit(tmp_path)
    assert D(result['closed_trades'][0]['initial_risk'])==10
    assert D(result['cost_sensitivity']['1']['expectancy_R'])==-1


def _audit_root(tmp_path, trades, fills, orders):
    import json
    import sqlite3
    (tmp_path/'runtime').mkdir();(tmp_path/'research').mkdir()
    c=sqlite3.connect(tmp_path/'runtime/journal.sqlite')
    c.executescript("""
      CREATE TABLE trades(day,symbol,direction,entry_price,stop_price,atr,state);
      CREATE TABLE fills(day,symbol,role,side,qty,price,ts,commission,slippage,order_id,cum_qty);
      CREATE TABLE candidates(day,symbol,details);
    """)
    for t in trades:
        c.execute('INSERT INTO trades VALUES(?,?,?,?,?,?,?)',(t[0],t[1],t[2],'0','0','1','active'))
        c.execute('INSERT INTO candidates VALUES(?,?,?)',(t[0],t[1],json.dumps({'entry':t[3],'stop':t[4]})))
    for f in fills:
        c.execute('INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?,?)',f)
    c.commit();c.close()
    (tmp_path/'research/broker_snapshot_20261003.json').write_text(json.dumps(
        {'observed_at':'test','positions':{},'orders':orders,'historical_access_probes':[]}))


def _order(cid, symbol, side, qty, price, filled_at):
    return {'id':cid,'client_order_id':cid,'symbol':symbol,'side':side,'filled_qty':qty,'filled_avg_price':price,
            'filled_at':filled_at}


def test_trades_whose_exit_the_local_journal_missed_are_not_dropped_from_the_headline(tmp_path):
    """Reported 0/25 omitted EFX (broker stop filled, local state stale `active`) and CBRS
    (short held overnight, exited by the next day's open sweep): both were losses."""
    from orb_iex.research_audit import audit
    _audit_root(tmp_path,
        [('2026-10-02','EFX',1,'141.21','140.70'),('2026-09-30','CBRS',-1,'177.86','178.95')],
        [('2026-10-02','EFX','entry','buy','96','141.25','2026-10-02T13:42:02Z','0.336','0.96','e1','96'),
         ('2026-09-30','CBRS','entry','sell','45','177.83','2026-09-30T19:59:29Z','0.1575','0.45','c1','45')],
        [_order('orbiex-20261002-EFX-stop1','EFX','sell','96','140.58','2026-10-02T15:12:52Z'),
         _order('orbiex-20261001-CBRS-opensweep1','CBRS','buy','45','183.685556','2026-10-01T13:33:48Z')])
    result=audit(tmp_path)
    assert result['excluded']==[]
    rows={r['symbol']:r for r in result['closed_trades']}
    assert {r['source'] for r in rows.values()}=={'broker_reconciled'}
    assert D(rows['EFX']['gross_pnl'])==D('-64.32')                          # (140.58-141.25)*96
    assert D(rows['CBRS']['gross_pnl'])==D('-263.50002')                     # (177.83-183.685556)*45 short
    assert D(rows['EFX']['modeled_cost'])==D('2.592')  # exit side is charged the entry's per-share costs
    assert result['cost_sensitivity']['1']['win_rate']==0 and result['cost_sensitivity']['1']['trades']==2


def test_trade_without_a_broker_exit_stays_excluded_rather_than_marked_to_market(tmp_path):
    from orb_iex.research_audit import audit
    _audit_root(tmp_path,[('2026-10-02','SPHD',-1,'48.05','48.15')],
        [('2026-10-02','SPHD','entry','sell','409','48.05','2026-10-02T15:25:07Z','1','4','s1','409')],[])
    result=audit(tmp_path)
    assert result['closed_trades']==[] and result['excluded'][0]['symbol']=='SPHD'
