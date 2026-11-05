import copy
from decimal import Decimal as D
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from olbot.close_positions import (SCOPE, Native, execute, make_leg, parse_ondo_close, parse_lighter_close)

ROOT=Path(__file__).parent


class Fake:
    scope_hash='test-only'
    def __init__(self,path,failure=None,partial=None):
        self.path,self.failure,self.partial=path,failure,partial
        self.op=dict(SCOPE);self.lp={b:-q for b,q in SCOPE.items()}
        self.sent=[]

    async def snapshot(self):
        return {'ondo_positions':{k:str(v) for k,v in self.op.items()},'lighter_positions':{k:str(v) for k,v in self.lp.items()},
                'ondo_open':0,'lighter_open':0,'pending':False,'ondo_equity':'250','lighter_equity':'250'}

    async def leg(self,base,venue,position):
        mid=D(3) if base=='LIT' else D('.18')
        tick=D('.0001') if base=='LIT' else D('.00001')
        return make_leg(base,venue,position,[(mid-tick,D(10000))],[(mid+tick,D(10000))],tick,D(1),D(1),D(0))

    def order_id(self,venue): return 'test-'+str(len(self.sent))

    async def send(self,leg,cid):
        # Every send must already have a durable pending record, including the first.
        state=json.loads(self.path.read_text(encoding='utf-8'))
        assert state['attempts'][-1]['client_id']==cid and state['attempts'][-1]['phase']=='send_unknown'
        self.sent.append((leg['base'],leg['venue']))
        if leg['base']=='ENA': assert self.op['LIT']==self.lp['LIT']==0

    async def confirm(self,leg,cid,before):
        key=(leg['base'],leg['venue'])
        size=D(leg['size'])
        if self.failure==key: size=D(0)
        if self.partial==key: size/=2
        pos=self.op if leg['venue']=='ondo' else self.lp
        pos[leg['base']]+=size if leg['side']=='buy' else -size
        return {'terminal':True,'status':'filled' if size==D(leg['size']) else 'canceled',
                'filled':str(size),'quote':str(size*D(leg['expected_vwap'])),'fee':'0'}


async def test_four_orders_sequential_and_journal_is_not_reusable(tmp_path,monkeypatch):
    monkeypatch.setattr('olbot.close_positions.asyncio.sleep',AsyncMock())
    path=tmp_path/'journal.json';f=Fake(path)
    s=await execute(f,path)
    assert s['phase']=='resolved' and s['orders_sent']==4
    assert f.sent==[('LIT','ondo'),('LIT','lighter'),('ENA','ondo'),('ENA','lighter')]
    assert not s['urgent'] and all(v==0 for v in list(f.op.values())+list(f.lp.values()))
    with pytest.raises(ValueError,match='never resend'): await execute(f,path)
    assert len(f.sent)==4


async def test_rejected_lighter_stops_with_one_sided_exposure_without_ena(tmp_path):
    path=tmp_path/'journal.json';f=Fake(path,failure=('LIT','lighter'))
    s=await execute(f,path)
    assert s['phase']=='halted' and s['urgent'] and s['orders_sent']==2
    assert f.op['LIT']==0 and f.lp['LIT']==-40 and all(b=='LIT' for b,v in f.sent)


async def test_explicit_rejection_halts_without_waiting_for_nonexistent_order(tmp_path):
    class Reject(Fake):
        async def send(self,leg,cid):
            await super().send(leg,cid)
            if leg['venue']=='lighter': return 'rejected'
        async def confirm(self,leg,*args):
            assert leg['venue']=='ondo'
            return await super().confirm(leg,*args)
    path=tmp_path/'journal.json';f=Reject(path)
    s=await execute(f,path)
    assert s['urgent'] and s['phase']=='halted' and s['orders_sent']==2


async def test_unknown_ondo_send_never_sends_lighter_or_retries(tmp_path):
    class Lost(Fake):
        async def send(self,leg,cid):
            await super().send(leg,cid)
            self.op['LIT']=D(0)
            raise TimeoutError('lost reply')
        async def confirm(self,*args): return None
    path=tmp_path/'journal.json';f=Lost(path)
    s=await execute(f,path)
    assert s['phase']=='halted' and s['urgent'] and s['orders_sent']==1
    assert f.sent==[('LIT','ondo')]


async def test_partial_ondo_closes_only_confirmed_amount_then_stops(tmp_path):
    path=tmp_path/'journal.json';f=Fake(path,partial=('LIT','ondo'))
    s=await execute(f,path)
    assert s['phase']=='halted' and not s['urgent'] and s['orders_sent']==2
    assert f.op['LIT']==20 and f.lp['LIT']==-20
    assert s['attempts'][1]['leg']['size']=='20' and s['attempts'][1]['leg']['position_before']=='-40'


async def test_position_mismatch_blocks_before_any_send(tmp_path):
    path=tmp_path/'journal.json';f=Fake(path);f.op['LIT']=D(41)
    s=await execute(f,path)
    assert s['phase']=='halted' and s['orders_sent']==0 and f.sent==[]


async def test_cost_budget_blocks_before_any_send(tmp_path):
    class Expensive(Fake):
        async def leg(self,*args):
            p=await super().leg(*args);p['worst_cost']='1';return p
    path=tmp_path/'journal.json';f=Expensive(path)
    s=await execute(f,path)
    assert s['phase']=='halted' and not s['urgent'] and s['orders_sent']==0


@pytest.mark.parametrize('position',[D(41),D(-40)])
def test_sizes_and_direction_cannot_increase_exposure(position):
    with pytest.raises(ValueError): make_leg('LIT','ondo',position,[(D(3),D(100))],[(D('3.01'),D(100))],D('.0001'),D(1),D(1),D(10))


def test_twenty_bps_protection_requires_full_depth_and_rounds_towards_safety():
    with pytest.raises(ValueError,match='depth'):
        make_leg('LIT','ondo',D(40),[(D(3),D(1)),(D('2.9'),D(100))],[(D('3.01'),D(100))],D('.0001'),D(1),D(1),D(10))
    p=make_leg('LIT','ondo',D(40),[(D('2.9999'),D(100))],[(D('3.01'),D(100))],D('.0001'),D(1),D(1),D(10))
    assert D(p['price_limit'])>=D('2.9999')*D('.998')


def test_real_ondo_close_fixture_requires_exact_reduce_only_and_quantities():
    raw=json.loads((ROOT/'fixtures/ondo-filled-ioc-close.json').read_text())
    leg={'side':'sell','size':'4','price_limit':raw['price']}
    result=parse_ondo_close(raw,leg,'fixture_close','LIT-USD.P')
    assert result['terminal'] and result['filled']=='4' and result['fee']=='0.003095'
    for key in ('reduceOnly','filledSize','fee'):
        bad=copy.deepcopy(raw);del bad[key]
        with pytest.raises((KeyError,ValueError)): parse_ondo_close(bad,leg,'fixture_close','LIT-USD.P')


def test_real_lighter_close_fixture_requires_reduce_only_and_correct_account():
    raw=json.loads((ROOT/'fixtures/lighter-filled-ioc-close.json').read_text())
    result=parse_lighter_close(raw,{'side':'buy','size':'4'},44,120,7)
    assert result['terminal'] and result['filled']=='4.00'
    bad=copy.deepcopy(raw);bad['reduce_only']=False
    with pytest.raises(ValueError): parse_lighter_close(bad,{'side':'buy','size':'4'},44,120,7)


async def test_sdk_send_is_reduce_only_and_uses_sdk_nonce_manager():
    native=object.__new__(Native)
    native.lm={'LIT':{'id':120,'size_decimals':2,'price_decimals':4}}
    create=AsyncMock(return_value=(None,SimpleNamespace(code=200),None))
    native.trader=SimpleNamespace(client=SimpleNamespace(create_market_order=create))
    await native.send({'venue':'lighter','base':'LIT','side':'buy','size':'40','price_limit':'3'},123)
    kwargs=create.call_args.kwargs
    assert kwargs['reduce_only'] is True and kwargs['base_amount']==4000 and kwargs['avg_execution_price']==30000
    assert 'api_key_index' not in kwargs
