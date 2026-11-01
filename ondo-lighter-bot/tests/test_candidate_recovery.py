import asyncio
import copy
from datetime import datetime,timezone
from decimal import Decimal as D
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from olbot.candidate_watch import CandidateWatch,CandidateOndoWS,parse_depth,session_label,BASES
from olbot.paper import Scenario
from olbot.paper_watch import Watch,write_report
from olbot.paper_recovery import reconciled_state
from olbot.public_budget import public_get
from olbot.wsbase import ReconnectingWS
from olbot.funding_report import price_proxies
from olbot.funding_watch import FundingWatch

ROOT=Path(__file__).parent.parent


def saved_virtual_run(folder,monkeypatch):
    monkeypatch.setattr('olbot.paper_watch.time.time',lambda:150)
    w=Watch(folder,'unused',1000);w.started=100
    for b in ('BTC','ETH'):
        for d in (5,10,18):
            for m in ('touch','through'):w.scenarios.append(Scenario(b,d,m,D('.01'),D('.01'),D(10),D('.01')))
    summary=w.summary(110);r=summary['rows'][0]
    r.update(fills=1,hedges=1,costed_fills=1,position='1',lighter_position='-1',maker_fees_usd='.01',fill_notional_usd='100',
             basis_cost_usd='-1',delay_cost_usd='0',spread_depth_cost_usd='0',accumulation_cost_usd='0',killed='preserved_risk_stop')
    fill={'base':'BTC','distance':5,'model':'touch','side':'buy','size':'1','price':'100','maker_fee':'.01','recv_ts':105}
    hedge={'base':'BTC','distance':5,'model':'touch','signed_size':'1','execution':'101','basis_usd':'-1','delay_usd':'0','spread_usd':'0','delay_sec':.4,'ts':106}
    w.write('summary.json',summary);w.write('fills.jsonl',fill,True);w.write('hedges.jsonl',hedge,True)
    return summary


def test_hedged_recovery_reconstructs_cash_inventory_costs_without_clearing_risk(tmp_path,monkeypatch):
    prior=saved_virtual_run(tmp_path,monkeypatch)
    restored=Watch(tmp_path,'unused',9999,resume_hedged=True)
    restored.scenarios=[Scenario(r['base'],r['distance_bps'],r['model'],D('.01'),D('.01'),D(10),D('.01')) for r in prior['rows']]
    restored.restore_flat_observations();s=restored.scenarios[0]
    assert (s.cash,s.pos,s.lpos)==(D('.99'),D(1),D(-1))
    assert s.maker_fees==D('.01') and s.basis_cost==D(-1) and s.fills==s.costed_fills==1
    assert s.funding_unknown_seconds==40 and s.killed=='preserved_risk_stop'
    assert not s.orders and s.pending is None and restored.deadline==1000
    assert json.loads((tmp_path/'summary.json').read_text(encoding='utf-8'))==prior


@pytest.mark.parametrize('change',[{'pending_hedge':True},{'fills':2},{'position':'2'},{'basis_cost_usd':'0'},{'costed_fills':0}])
def test_recovery_refuses_partial_or_inconsistent_virtual_journals(tmp_path,monkeypatch,change):
    prior=saved_virtual_run(tmp_path,monkeypatch);prior['rows'][0].update(change)
    with pytest.raises(ValueError):reconciled_state(tmp_path,prior)


def test_event_after_checkpoint_blocks_recovery(tmp_path,monkeypatch):
    prior=saved_virtual_run(tmp_path,monkeypatch)
    p=tmp_path/'fills.jsonl';r=json.loads(p.read_text());r['recv_ts']=111;p.write_text(json.dumps(r)+'\n')
    with pytest.raises(ValueError,match='after saved'):reconciled_state(tmp_path,prior)


def test_actual_depth_snapshot_and_public_only_ws_subscriptions(tmp_path):
    raw=json.loads((ROOT/'tests/fixtures/candidate-depth-public.json').read_text());rows=parse_depth(raw)
    assert rows and rows[0][0]=='HOOD' and D(rows[0][1]['bids'][0][0])<D(rows[0][1]['asks'][0][0])
    bad=copy.deepcopy(raw);bad['data'][0]['bids'][0][0]='NaN'
    with pytest.raises(ValueError):parse_depth(bad)
    w=CandidateWatch(tmp_path,'unused',1000);w.meta={b:{'market':b+'-USD.P'} for b in BASES}
    assert isinstance(w.ondo_ws,ReconnectingWS)
    assert all(x['op']=='subscribe' for x in w.ondo_ws.subscriptions())
    assert w.ondo_ws.subscriptions()[-1]['channel']=='depthBooksPerps'


def test_candidate_models_do_not_share_one_capital_pool_or_sum_returns(tmp_path):
    w=CandidateWatch(tmp_path,'unused',1000);w.started=100
    for b in BASES:
        for d in (5,10,18):
            for m in ('touch','through'):w.scenarios.append(Scenario(b,d,m,D('.01'),D('.01'),D(10),D('.01')))
    assert len(w.group(w.scenarios[0]))==1
    summary=w.summary(200)
    for r in summary['rows']:r['net_week_low_usdc']=r['net_week_high_usdc']='.5'
    w.write('summary.json',summary);write_report(tmp_path)
    report=(tmp_path/'REPORT.md').read_text(encoding='utf-8')
    assert '# 18개 조건' in report and report.count('가정 범위에서 기준 미달')==18
    assert '가정 범위에서 기준 이상' not in report and '관측 7일 미만' in report


def test_us_session_dst_holiday_and_commodity_are_not_mislabeled():
    stamp=lambda x:datetime.fromisoformat(x).timestamp()
    assert session_label('META',stamp('2026-10-30T13:30:00+00:00'))=='regular'
    assert session_label('META',stamp('2026-11-02T13:30:00+00:00'))=='outside_regular'
    assert session_label('META',stamp('2026-11-02T14:30:00+00:00'))=='regular'
    assert session_label('META',stamp('2026-11-01T15:00:00+00:00'))=='weekend'
    assert session_label('NVDA',stamp('2026-11-26T15:00:00+00:00'))=='holiday'
    assert session_label('XAU',stamp('2026-11-02T14:30:00+00:00'))=='commodity_24h_session_unclassified'


def test_candidate_funding_uses_own_window_and_shared_budget(tmp_path):
    comparison=tmp_path/'comparison';comparison.mkdir()
    launch={'started_utc':'2026-11-01T06:00:00+00:00','deadline_utc':'2026-11-06T11:43:35+00:00',
            'markets':list(BASES),'rest_coordination':str(tmp_path/'shared')}
    p=tmp_path/'comparison-launch.json';p.write_text(json.dumps(launch))
    w=FundingWatch(comparison,tmp_path/'funding')
    assert w.bases==BASES and set(w.history)==set(BASES) and w.coordination==tmp_path/'shared'
    launch['markets']=['NOT_VERIFIED'];p.write_text(json.dumps(launch))
    with pytest.raises(ValueError):FundingWatch(comparison,tmp_path/'funding')


def test_candidate_depth_disconnect_clears_book_instead_of_reusing_epoch(tmp_path):
    w=CandidateWatch(tmp_path,'unused',1000);w.depths={'META':({},100)}
    w.event('paper_ondo_ws','closed')
    assert not w.depths


async def test_public_budget_respects_pause_owner_and_cleans_up(tmp_path):
    pause=tmp_path/'PAUSE_REST';pause.write_text('owner')
    caller=asyncio.create_task(public_get('https://api.ondoperps.xyz/v1/markets',tmp_path,getter=lambda _:1,spacing=.01))
    await asyncio.sleep(.03);assert not caller.done()
    assert await public_get('https://api.ondoperps.xyz/v1/markets',tmp_path,owner='owner',getter=lambda _:2,spacing=.01)==2
    pause.unlink();assert await caller==1
    assert not (tmp_path/'REST_ACTIVE').exists()
    with pytest.raises(ValueError):await public_get('https://api.ondoperps.xyz/v1/perps/orders',tmp_path)


async def test_cancelled_get_keeps_budget_lock_until_worker_thread_finishes(tmp_path):
    started=__import__('threading').Event()
    def slow(_):started.set();time.sleep(.2);return 1
    task=asyncio.create_task(public_get('https://api.ondoperps.xyz/v1/markets',tmp_path,getter=slow,spacing=.01))
    for _ in range(100):
        if started.is_set():break
        await asyncio.sleep(.005)
    assert started.is_set();task.cancel();await asyncio.sleep(.02)
    assert (tmp_path/'REST_ACTIVE').exists()
    with pytest.raises(asyncio.CancelledError):await task
    assert not (tmp_path/'REST_ACTIVE').exists()


def test_separate_processes_share_real_one_second_public_budget(tmp_path):
    code="""import asyncio,json,os,sys,time
from pathlib import Path
from olbot.public_budget import public_get
p=Path(sys.argv[1])
def get(url):
 with (p/'calls.jsonl').open('a') as s:s.write(json.dumps({'ts':time.time(),'pid':os.getpid()})+'\\n')
 time.sleep(.03)
 return 1
async def main():
 for _ in range(2):await public_get('https://api.ondoperps.xyz/v1/markets',p,getter=get)
asyncio.run(main())
"""
    env={**os.environ,'PYTHONPATH':str(ROOT/'src')}
    children=[subprocess.Popen([sys.executable,'-c',code,str(tmp_path)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
    for child in children:
        out,err=child.communicate(timeout=20);assert child.returncode==0,err.decode()
    calls=[json.loads(x) for x in (tmp_path/'calls.jsonl').read_text().splitlines()]
    stamps=sorted(x['ts'] for x in calls)
    assert len(calls)==4 and len({x['pid'] for x in calls})==2
    assert all(b-a>=1.08 for a,b in zip(stamps,stamps[1:]))
