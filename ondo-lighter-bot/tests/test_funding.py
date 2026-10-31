import copy
from datetime import datetime,timezone
from decimal import Decimal as D
import gzip
import json
from pathlib import Path

import pytest
from unittest.mock import AsyncMock

from olbot.funding import (parse_ondo_history,parse_lighter_history,hour_stamp,make_hours,summarize,paired_fee_credit,history_window)
from olbot.funding_report import scenario_funding,hold_result
from olbot.funding_watch import cost_snapshot
from olbot.paper import Book,Scenario
from olbot.paper_watch import Watch,write_report
from olbot.public_lease import public_rest_lease

ROOT=Path(__file__).parent.parent
FIX=ROOT/'tests/fixtures/funding-public'


def raw(name): return json.loads((FIX/name).read_text(encoding='utf-8'))


def test_actual_hourly_units_and_lighter_sign_match_actual_ws_settlement():
    history=parse_lighter_history(raw('lighter-ETH-history.json'))
    snapshot=json.loads((ROOT/'tests/fixtures/paper-public/lighter-subscribed-market_stats-market_stats-0.json').read_text(encoding='utf-8'))
    # Same ETH market and settlement hour in the independent actual WS sample.
    h=hour_stamp(snapshot['market_stats']['funding_timestamp']//1000)
    assert D(history[h]['rate'])==D(snapshot['market_stats']['funding_rate'])/100==D('.000012')
    eth=parse_lighter_history(raw('lighter-ETH-history.json'))
    negative=next(row for row in eth.values() if row['direction']=='short')
    assert D(negative['rate'])==D('-.000012') and D(negative['per_base_value'])<0
    ondo=parse_ondo_history(raw('ondo-BTC-history.json'),'BTC-USD.P')
    assert D(ondo[1793361600]['rate'])==D('-.0000485')
    assert ondo[1793361600]['settled_ts']>1793361600  # actual first tick, not rounded settlement


@pytest.mark.parametrize('change',[{'rate':'NaN'},{'direction':'unknown'},{'rate':'-.001'},{'timestamp':1793358000000}])
def test_unknown_sign_units_or_nonfinite_rate_do_not_become_zero(change):
    data=raw('lighter-BTC-history.json');data['fundings'][0].update(change)
    with pytest.raises((ValueError,ArithmeticError)): parse_lighter_history(data)


def test_missing_required_rate_and_incomplete_ondo_page_rejected():
    data=raw('ondo-BTC-history.json');del data['result'][0]['fundingRate']
    with pytest.raises(KeyError): parse_ondo_history(data,'BTC-USD.P')
    data=raw('ondo-BTC-history.json');data['pageInfo']['nextCursor']='more'
    with pytest.raises(ValueError): parse_ondo_history(data,'BTC-USD.P')


def test_hour_grid_marks_missing_instead_of_imputing_zero():
    start=1793358000+43*60
    o=parse_ondo_history(raw('ondo-BTC-history.json'),'BTC-USD.P')
    l=parse_lighter_history(raw('lighter-BTC-history.json'))
    rows=make_hours('BTC',o,l,start,1793368800+10,1793368800+10)
    assert len(rows)==3 and rows[0]['difference'] is not None
    assert rows[-1]['difference'] is None and rows[-1]['missing']==['lighter']
    assert rows[-1]['lighter_rate'] is None


def test_count_back_extra_history_and_future_do_not_leak_into_window():
    parsed=parse_lighter_history(raw('lighter-BTC-history.json'))
    selected=history_window(parsed,1793354400,1793358000)
    assert list(sorted(selected))==[1793354400,1793358000]
    assert 1793350800 not in selected and 1793361600 not in selected


def test_gaps_and_zero_break_sign_streaks_and_fraction_uses_only_paired_hours():
    h=1793358000
    rows=[{'hour':h+i*3600,'difference':v} for i,v in enumerate(['.001','.001',None,'.001','0','-.001'])]
    s=summarize(rows,'.001')
    assert s['hours_paired']==5 and s['hours_missing']==1
    assert s['longest_positive_hourly_streak']==2 and s['longest_negative_hourly_streak']==1
    assert D(s['dominant_sign_fraction'])==D('.6')
    assert s['fixed_200_full_window_usdc'] is None
    assert s['fixed_200_known_subtotal_usdc']=='0.400'


def test_direction_fixed_by_prior_signal_not_future_profitable_sign():
    rows=[{'hour':1793358000+i*3600,'difference':'.001'} for i in range(2)]
    s=summarize(rows,'-.001')
    assert s['fixed_direction']=='ondo_long_lighter_short'
    assert D(s['fixed_200_full_window_usdc'])==D('-.4')
    assert summarize(rows,'0')['fixed_direction'] is None


def test_zero_is_distinct_from_missing_and_cash_flow_signs_are_correct():
    q=D('.0025')
    # Ondo negative funding pays longs; Lighter positive funding pays shorts.
    assert paired_fee_credit(q,-q,D(77000),D('-.00004'),D('.93'))==D('.010025')
    assert paired_fee_credit(q,-q,None,D(0),D(0)) is None
    assert paired_fee_credit(q,-q,D(77000),D(0),D(0))==0
    assert paired_fee_credit(D(0),D(0),None,None,None)==0  # known no inventory, not a fabricated rate


def test_each_venue_uses_its_actual_settlement_instant_without_future_inventory(tmp_path):
    h=1793358000
    with gzip.open(tmp_path/'reward-inputs.jsonl.gz','wt',encoding='utf-8') as stream:
        stream.write(json.dumps({'base':'BTC','ts':h-20,'book':{'bids':[['99','10']],'asks':[['101','10']]}})+'\n')
    (tmp_path/'fills.jsonl').write_text(json.dumps({'base':'BTC','distance':5,'model':'touch','recv_ts':h+.2,'side':'buy','size':'1'})+'\n')
    (tmp_path/'hedges.jsonl').write_text(json.dumps({'base':'BTC','distance':5,'model':'touch','ts':h+.6,'signed_size':'1'})+'\n')
    hourly=[{'base':'BTC','hour':h,'ondo_settled_ts':h+1.1,'lighter_settled_ts':h,
             'ondo_rate':'.001','lighter_per_base_value':'.2','missing':[]}]
    f=scenario_funding(tmp_path,hourly,'BTC',5,'touch',h+2)
    assert D(f['credit_usdc'])==D('-.1')
    assert f['known_hours'][0]['ondo_position']=='1' and f['known_hours'][0]['lighter_position']=='0'


def test_entry_exit_friction_is_not_free_and_basis_change_does_not_cancel():
    book=Book();book.apply(True,1,None,[[(D('99.9'),D(10))],[(D('100.1'),D(10))]],1)
    original=cost_snapshot('BTC',{'bids':[['99.9','10']],'asks':[['100.1','10']]},book,D(1),1,'test')
    assert D(original['-1']['entry_cost'])>0 and D(original['-1']['exit_cost'])>0
    later=copy.deepcopy(original);later['ondo_mid']='101';later['lighter_mid']='100.5'
    r=hold_result('BTC',[],{'fixed_direction':'ondo_long_lighter_short'},original,later,[])
    assert D(r['basis_mark_pnl_usdc'])==D('.5')
    assert D(r['funding_less_entry_exit_usdc'])<0


def test_report_replaces_funding_forecast_instead_of_double_adding(tmp_path):
    comparison=tmp_path/'comparison';comparison.mkdir();funding=tmp_path/'funding';funding.mkdir()
    policy=json.loads((ROOT/'docs/rewards-2026-10-27.json').read_text(encoding='utf-8'))
    start=datetime(2026,10,30,11,tzinfo=timezone.utc).timestamp()
    policies=comparison/'policies';policies.mkdir();(policies/'valid.json').write_text(json.dumps(policy))
    with gzip.open(comparison/'reward-inputs.jsonl.gz','wt',encoding='utf-8') as stream:
        for base in ('BTC','ETH'):
            for delta in (10,70):
                sample={'base':base,'ts':start+delta,'book':{'bids':[['99.99','100']],'asks':[['100.01','100']]},
                        'orders':[{'distance':d,'model':m,'levels':[]} for d in (5,10,18) for m in ('touch','through')]}
                stream.write(json.dumps(sample)+'\n')
    w=Watch(comparison,'unused',start+604800);w.started=start
    for base in ('BTC','ETH'):
        for d in (5,10,18):
            for m in ('touch','through'): w.scenarios.append(Scenario(base,d,m,D('.01'),D('.01'),D(10),D('.01')))
    data=w.summary(start+100)
    for r in data['rows']:
        r['mark_pnl_ex_reward_funding_usd']='1';r['funding_estimate_usd']='100';r['exit_cost_estimate_usd']='.1'
    w.write('summary.json',data)
    (funding/'hourly-latest.json').write_text(json.dumps({'updated_ts':start+100,'rows':[],'seeds':{}}))
    (funding/'entry-cost-proxies.json').write_text('{}')
    write_report(comparison)
    result=json.loads((comparison/'report-data.json').read_text(encoding='utf-8'))
    assert all(D(r['net_observed_low_usdc'])==D('.9') for r in result['rows'])
    assert all(r['funding_forecast_original_usd']=='100' and r['funding_settled_credit_usdc']=='0' for r in result['rows'])


async def test_rest_lease_releases_its_marker_even_on_failure(tmp_path,monkeypatch):
    monkeypatch.setattr('olbot.public_lease.asyncio.sleep',AsyncMock())
    with pytest.raises(RuntimeError):
        async with public_rest_lease(tmp_path):
            assert (tmp_path/'PAUSE_REST').exists()
            raise RuntimeError('read failure')
    assert not (tmp_path/'PAUSE_REST').exists()


async def test_rest_lease_does_not_remove_another_owners_pause(tmp_path):
    (tmp_path/'PAUSE_REST').write_text('other operation')
    with pytest.raises(TimeoutError):
        async with public_rest_lease(tmp_path,timeout=0): pass
    assert (tmp_path/'PAUSE_REST').read_text()=='other operation'
