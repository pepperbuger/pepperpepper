import json
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from olbot.paper import Scenario
from olbot.paper_watch import Watch, write_report
from olbot.funding_watch import FundingWatch


def previous_run(folder, monkeypatch):
    monkeypatch.setattr('olbot.paper_watch.time.time', lambda: 150)
    w = Watch(folder, 'unused', 1000)
    w.started = 100
    for b in ('BTC', 'ETH'):
        for d in (5, 10, 18):
            for m in ('touch', 'through'):
                w.scenarios.append(Scenario(b,d,m,D('.01'),D('.01'),D(10),D('.01')))
    data = w.summary(110)
    for r in data['rows']:
        r['qualified_data_seconds'] = 9
        r['data_gap_seconds'] = 1
    w.write('summary.json', data)
    return data


def test_flat_resume_preserves_window_counts_stop_and_marks_unobserved_interval(tmp_path,monkeypatch):
    prior = previous_run(tmp_path,monkeypatch)
    prior['rows'][0]['killed'] = 'risk_stop'
    (tmp_path/'summary.json').write_text(json.dumps(prior))
    reward = dict(ts=105,base='BTC',distance=5,model='touch',low_week_usdc='2',high_week_usdc='3',interpolation_sec=60)
    (tmp_path/'rewards.jsonl').write_text(json.dumps(reward)+'\n')
    resumed = Watch(tmp_path,'unused',9999,resume_flat=True)
    resumed.scenarios = [Scenario(s['base'],s['distance_bps'],s['model'],D('.01'),D('.01'),D(10),D('.01')) for s in prior['rows']]
    resumed.restore_flat_observations()
    s=resumed.scenarios[0]
    assert resumed.started == 100 and resumed.deadline == 1000
    assert resumed.interruption_gaps[0]['start_ts']==110
    assert s.eligible_seconds==9 and s.data_gap_seconds==41
    assert s.killed=='risk_stop' and not s.orders and s.pending is None
    assert s.reward_values==[D(2)] and s.reward_accrued_low==D(120)/604800
    assert s.reward_unknown==1 and resumed.reward_ts is None
    assert (tmp_path/'summary.json').read_text()==json.dumps(prior)


@pytest.mark.parametrize('change',[
    {'position':'1'}, {'lighter_position':'-1'}, {'pending_hedge':True},
    {'fills':1}, {'hedges':1}, {'maker_fees_usd':'.01'},
])
def test_resume_refuses_nonflat_or_incomplete_virtual_state(tmp_path,monkeypatch,change):
    prior=previous_run(tmp_path,monkeypatch)
    prior['rows'][0].update(change)
    (tmp_path/'summary.json').write_text(json.dumps(prior))
    with pytest.raises(ValueError,match='0이 아님'):
        Watch(tmp_path,'unused',9999,resume_flat=True)


@pytest.mark.parametrize('name',['fills.jsonl','hedges.jsonl'])
def test_resume_refuses_event_written_after_last_zero_summary(tmp_path,monkeypatch,name):
    previous_run(tmp_path,monkeypatch)
    (tmp_path/name).write_text('{"ts":111}\n')
    with pytest.raises(ValueError,match='체결 이력'):
        Watch(tmp_path,'unused',9999,resume_flat=True)


def test_resume_preserves_original_fixture_and_full_window_profit_is_unknown(tmp_path,monkeypatch):
    prior=previous_run(tmp_path,monkeypatch)
    w=Watch(tmp_path,'unused',9999,resume_flat=True)
    msg={'type':'update','channel':'topOfBooksPerps','data':['original']}
    w.fixture('ondo',msg)
    saved=next((tmp_path/'fixtures').iterdir()).read_text()
    msg['data']=['new'];w.fixture_keys.clear();w.fixture('ondo',msg)
    assert next((tmp_path/'fixtures').iterdir()).read_text()==saved
    prior['interruption_gaps']=w.interruption_gaps
    for r in prior['rows']:
        r['net_week_low_usdc']='10';r['net_week_high_usdc']='20'
    (tmp_path/'summary.json').write_text(json.dumps(prior))
    write_report(tmp_path)
    report=json.loads((tmp_path/'report-data.json').read_text(encoding='utf-8'))
    assert all(r['net_week_low_usdc'] is None and r['net_observed_low_usdc'] is None for r in report['rows'])
    assert '자료 부족' in (tmp_path/'REPORT.md').read_text(encoding='utf-8')


def test_hourly_restart_keeps_original_history_first_seen_and_error_counts(tmp_path):
    comparison=tmp_path/'comparison';comparison.mkdir()
    output=tmp_path/'funding';output.mkdir()
    iso=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
    (tmp_path/'comparison-launch.json').write_text(json.dumps({'started_utc':iso(100),'deadline_utc':iso(1000)}))
    old={'start_ts':100.0,'end_ts':1000.0,'errors':2,
         'history':{b:{v:{'0':{'first_seen':105,'last_seen':106,'data':{'rate':'.001'}}} for v in ('ondo','lighter')} for b in ('BTC','ETH')}}
    (output/'hourly-latest.json').write_text(json.dumps(old))
    (output/'status.json').write_text(json.dumps({'requests':12}))
    w=FundingWatch(comparison,output)
    assert w.history['BTC']['ondo'][0]['first_seen']==105
    assert w.calls==12 and w.errors==2
    old['end_ts']=2000
    (output/'hourly-latest.json').write_text(json.dumps(old))
    with pytest.raises(ValueError,match='window mismatch'): FundingWatch(comparison,output)
