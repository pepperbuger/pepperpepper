"""실제 주문 없이 단발 시험의 중복 방지·체결 대사·정리 한도를 검사한다."""
from decimal import Decimal as D
import json
from types import SimpleNamespace

import pytest

from olbot import paired_trial as pair
from olbot.hedger import PendingHedge
from olbot.ledger import Ledger, pending_scope
from olbot.lighter.client import LighterMarket, OrderStatus


MARKET=LighterMarket('LIT',17,2,4,D('.01'),D('10'))


def plan():
    return {**pair.calculate_plan({'base_increment':'1','quote_increment':'.0001'},
                                  MARKET,D('2.99'),D('3'),D('3'),D('3.01')),'ready':True}


def test_plan_uses_smallest_shared_unit_above_hedger_minimum():
    p=plan()
    assert D(p['size'])==4
    assert D(p['ondo_notional_cap_usd'])<=15
    assert (D(p['size'])-1)<MARKET.min_hedge_size(D('3.005'))


@pytest.mark.parametrize('change',[
    {'base_increment':'NaN'},{'base_increment':'0'},{'quote_increment':'-1'},
])
def test_invalid_increments_refuse_plan(change):
    with pytest.raises(ValueError):
        pair.calculate_plan({'base_increment':'1','quote_increment':'.0001',**change},
                            MARKET,D('2.99'),D('3'),D('3'),D('3.01'))


@pytest.mark.parametrize('prices',[
    ('NaN','3','3','3.01'),('3','2','3','3.01'),('0','3','3','3.01'),
    ('2.99','3','4','4.01'),('4.99','5','4.99','5.01'),
])
def test_invalid_book_basis_or_cap_refuses_plan(prices):
    with pytest.raises(ValueError):
        pair.calculate_plan({'base_increment':'1','quote_increment':'.0001'},MARKET,*map(D,prices))


def fixture(tmp_path,monkeypatch,*,ondo_fill='4',lighter_fill='4',lose=False,unknown=False,partial_close=False):
    path=tmp_path/'paired-hedge-trial.json'
    state={'phase':'prepared','plan':plan(),'ondo_entry_id':'entry','lighter_entry_attempted':False}
    pair.lighter.save(path,state,first=True)
    ledger=Ledger(path.with_suffix('.sqlite'))

    class Rest:
        def __init__(self):
            self.calls=[]
            self.position=D(0)
            self.orders={}
        async def request(self,method,url,*,payload=None,params=None):
            if method=='POST':
                saved=json.loads(path.read_text())
                closing=payload['reduceOnly']
                assert payload['clientOrderId']==saved['ondo_close_id' if closing else 'ondo_entry_id']
                assert payload['timeInForce']=='IOC' and payload['postOnly'] is False
                self.calls.append(payload)
                filled=D(payload['size']) if closing else D(ondo_fill)
                if closing and partial_close:
                    filled=D('1')
                self.position+= -filled if closing else filled
                self.orders[payload['clientOrderId']]={**payload,'filledSize':str(filled),
                    'status':'fullyfilled' if filled==D(payload['size']) else 'canceled'}
                if lose:
                    raise OSError('response lost')
                return self.orders[payload['clientOrderId']]
            if url.startswith(pair.ondo.ORDER_PATH):
                return self.orders[url.removeprefix(pair.ondo.ORDER_PATH)]
            assert method=='GET' and url=='/v1/perps/orders'
            return []

    class Trader:
        account_index=7
        def __init__(self):
            self.client=self
            self.orders={}
            self.calls=[]
            self.position=D(0)
            self.coi=40
        def _auth(self):
            return 'test-token'
        def new_client_order_index(self):
            self.coi+=1
            return self.coi
        async def positions(self):
            return {17:self.position}
        async def account(self):
            return {'account_index':7,'positions':[{'market_id':17,'position':str(abs(self.position)),
                'sign':-1 if self.position<0 else 1}]}
        async def market_order(self,m,size,is_sell,worst,client_order_index=None):
            saved=json.loads(path.read_text())
            assert saved['lighter_entry_id']==client_order_index and saved['lighter_entry_attempted']
            assert ledger.load_pending_hedges(pending_scope(pair.lighter.BASE,7))['LIT'].client_order_index==client_order_index
            assert m==MARKET and is_sell
            self.calls.append({'client_order_index':client_order_index,'reduce_only':False})
            if unknown:
                raise OSError('outcome unknown')
            self.position-=D(lighter_fill)
            self.orders[client_order_index]=OrderStatus(client_order_index,
                'filled' if D(lighter_fill)==size else 'canceled',D(lighter_fill),size)
            if lose:
                raise OSError('response lost')
        async def order_status(self,coi):
            return self.orders.get(coi)
        async def create_market_order(self,**kwargs):
            saved=json.loads(path.read_text())
            assert saved['lighter_close_id']==kwargs['client_order_index']
            assert kwargs['reduce_only'] is True and kwargs['is_ask'] is False
            assert 'api_key_index' not in kwargs
            assert kwargs['base_amount']==D(lighter_fill)*100
            assert not ledger.load_pending_hedges(pending_scope(pair.lighter.BASE,7))
            self.calls.append(kwargs)
            size=D(kwargs['base_amount'])/100
            self.position+=size
            self.orders[kwargs['client_order_index']]=OrderStatus(kwargs['client_order_index'],'filled',size,size)
            if lose:
                raise OSError('response lost')

    rest,trader=Rest(),Trader()
    async def positions(rest):
        return {pair.ondo.MARKET:rest.position}
    async def quote():
        return D('2.99'),D('3')
    async def book(market_id):
        return D('3'),D('3.01')
    async def final(trader,p,coi,is_sell,timeout=30):
        order=trader.orders.get(coi)
        if order is not None:
            before=D(p.get('position_before','0'))
            assert trader.position==before+(-order.filled if is_sell else order.filled)
            assert D(p['size'])==order.initial
        return order
    async def get(url,token=None):
        assert 'accountActiveOrders' in url
        return {'orders':[]}
    monkeypatch.setattr(pair.ondo,'positions',positions)
    monkeypatch.setattr(pair.ondo,'quote',quote)
    monkeypatch.setattr(pair,'lighter_book',book)
    monkeypatch.setattr(pair.lighter,'final_order',final)
    monkeypatch.setattr(pair.lighter,'get',get)
    return rest,trader,state,path,ledger


@pytest.mark.parametrize('lose',[False,True])
async def test_positive_fills_use_real_hedger_and_only_four_total_placements(tmp_path,monkeypatch,lose):
    rest,trader,state,path,ledger=fixture(tmp_path,monkeypatch,lose=lose)
    try:
        result=await pair.execute(rest,trader,state,path,ledger)
        assert result['resolved'] and result['hedge_verified']
        assert rest.position==trader.position==0
        assert len(rest.calls)==len(trader.calls)==2
        assert rest.calls[1]['reduceOnly'] is True
        assert not ledger.load_pending_hedges(pending_scope(pair.lighter.BASE,7))
        # 같은 기록을 다시 정리해도 기존 주문 번호 조회만 한다.
        assert (await pair.finish(rest,trader,state,path))['resolved']
        assert len(rest.calls)==len(trader.calls)==2
    finally:
        ledger.close()


async def test_unknown_hedge_blocks_cleanup_and_duplicate_hedge(tmp_path,monkeypatch):
    rest,trader,state,path,ledger=fixture(tmp_path,monkeypatch,unknown=True)
    try:
        assert not (await pair.execute(rest,trader,state,path,ledger))['resolved']
        assert len(rest.calls)==len(trader.calls)==1
        assert ledger.load_pending_hedges(pending_scope(pair.lighter.BASE,7))
        assert not (await pair.finish(rest,trader,state,path))['resolved']
        proxy=pair.RecordedTrader(trader,state,path)
        with pytest.raises(RuntimeError,match='1회'):
            await proxy.market_order(MARKET,D(4),True,D(3),client_order_index=99)
        assert len(trader.calls)==1
    finally:
        ledger.close()


@pytest.mark.parametrize('ondo_fill,lighter_fill,expected',[('0','0',0),('2','0',0),('4','2',2),('4','0',1)])
async def test_zero_and_partial_fills_never_retry_entry(tmp_path,monkeypatch,ondo_fill,lighter_fill,expected):
    rest,trader,state,path,ledger=fixture(tmp_path,monkeypatch,ondo_fill=ondo_fill,lighter_fill=lighter_fill)
    try:
        result=await pair.execute(rest,trader,state,path,ledger)
        assert result['resolved'] and result['hedge_verified'] is False
        assert len(trader.calls)==expected
        assert len(rest.calls)==(1 if ondo_fill=='0' else 2)
        assert rest.position==trader.position==0
    finally:
        ledger.close()


async def test_partial_cleanup_is_not_retried_or_mistaken_for_flat(tmp_path,monkeypatch):
    rest,trader,state,path,ledger=fixture(tmp_path,monkeypatch,partial_close=True)
    try:
        assert not (await pair.execute(rest,trader,state,path,ledger))['resolved']
        assert not (await pair.finish(rest,trader,state,path))['resolved']
        assert len(rest.calls)==2 and len(trader.calls)==1
        assert rest.position==3 and trader.position==-4
    finally:
        ledger.close()


async def test_journal_failure_blocks_first_order(tmp_path,monkeypatch):
    rest,trader,state,path,ledger=fixture(tmp_path,monkeypatch)
    def fail(*args,**kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(pair.lighter,'save',fail)
    try:
        with pytest.raises(OSError):
            await pair.execute(rest,trader,state,path,ledger)
        assert rest.calls==trader.calls==[]
    finally:
        ledger.close()


async def test_sql_failure_keeps_journal_and_prevents_hedge_transmission(tmp_path,monkeypatch):
    rest,trader,state,path,ledger=fixture(tmp_path,monkeypatch)
    def fail(*args,**kwargs):
        raise OSError('SQL commit failed')
    monkeypatch.setattr(ledger,'save_pending_hedge',fail)
    try:
        # Hedger catches storage errors; the probe may close only the known Ondo fill.
        result=await pair.execute(rest,trader,state,path,ledger)
        assert result['resolved'] and not result['hedge_verified']
        assert trader.calls==[] and len(rest.calls)==2
        assert json.loads(path.read_text())['lighter_entry_id']==41
    finally:
        ledger.close()


async def test_unknown_ondo_order_never_sends_hedge_or_cleanup(tmp_path,monkeypatch):
    rest,trader,state,path,ledger=fixture(tmp_path,monkeypatch)
    async def unknown(*args,**kwargs):
        return None
    monkeypatch.setattr(pair,'wait_ondo',unknown)
    try:
        assert not (await pair.execute(rest,trader,state,path,ledger))['resolved']
        assert len(rest.calls)==1 and trader.calls==[]
        assert state['phase']=='ondo_entry_pending'
    finally:
        ledger.close()


@pytest.mark.parametrize('phase,verified',[('ondo_entry_pending',False),('resolved',False)])
def test_guard_refuses_unknown_or_unverified_trial(tmp_path,phase,verified):
    cfg=SimpleNamespace(storage=SimpleNamespace(sqlite_path=tmp_path/'olbot.sqlite'))
    path,db=pair.paths(cfg)
    pair.lighter.save(path,{'phase':phase,'hedge_verified':verified},first=True)
    with pytest.raises(RuntimeError):
        pair.require_resolved_trial(cfg)


def test_missing_journal_does_not_bypass_database_guard(tmp_path):
    cfg=SimpleNamespace(storage=SimpleNamespace(sqlite_path=tmp_path/'olbot.sqlite'))
    path,db=pair.paths(cfg)
    db.touch()
    with pytest.raises(RuntimeError):
        pair.require_resolved_trial(cfg)


def test_resolved_json_cannot_bypass_remaining_sql_pending(tmp_path):
    cfg=SimpleNamespace(storage=SimpleNamespace(sqlite_path=tmp_path/'olbot.sqlite'))
    path,db=pair.paths(cfg)
    pair.lighter.save(path,{'phase':'resolved','hedge_verified':True},first=True)
    ledger=Ledger(db)
    ledger.save_pending_hedge(pending_scope(pair.lighter.BASE,7),'LIT',PendingHedge(D(4),True,D(0),1,41))
    ledger.close()
    with pytest.raises(RuntimeError,match='DB에 미확정'):
        pair.require_resolved_trial(cfg)


@pytest.mark.parametrize('limit,cleanup',[ (None,False),('15',False),('16',True)])
async def test_execution_approval_flags_checked_before_connection(tmp_path,monkeypatch,limit,cleanup):
    cfg=SimpleNamespace(storage=SimpleNamespace(sqlite_path=tmp_path/'olbot.sqlite'),
        ondo=SimpleNamespace(rest_url=pair.ondo.BASE),lighter=SimpleNamespace(base_url=pair.lighter.BASE))
    secrets=SimpleNamespace(lighter_account_index='7')
    monkeypatch.setattr(pair,'load_config',lambda path:cfg)
    monkeypatch.setattr(pair,'load_secrets',lambda path:secrets)
    args=SimpleNamespace(config='unused',env='unused',execute=True,finish_known=False,
        approve_max_position_usd=limit,approve_cleanup=cleanup)
    with pytest.raises(ValueError,match='승인 플래그'):
        await pair.run(args)


def setup_run(tmp_path,monkeypatch,execute=False):
    cfg=SimpleNamespace(storage=SimpleNamespace(sqlite_path=tmp_path/'olbot.sqlite'),
        ondo=SimpleNamespace(rest_url=pair.ondo.BASE),lighter=SimpleNamespace(base_url=pair.lighter.BASE))
    secrets=SimpleNamespace(ondo_api_key_id='dummy',ondo_api_secret='dummy',lighter_account_index='7',lighter_api_key_index='4',
                            lighter_api_key_private_key='dummy')
    monkeypatch.setattr(pair,'load_config',lambda path:cfg)
    monkeypatch.setattr(pair,'load_secrets',lambda path:secrets)
    args=SimpleNamespace(config='unused',env='unused',execute=execute,finish_known=False,
                        approve_max_position_usd='15',approve_cleanup=True)
    return cfg,secrets,args


async def test_default_only_queries_plan_without_creating_state_or_database(tmp_path,monkeypatch):
    cfg,secrets,args=setup_run(tmp_path,monkeypatch)
    class Trader:
        def __init__(self,*args):
            pass
        async def connect(self):
            pass
        async def close(self):
            pass
    async def preflight(*args):
        return plan()
    async def refuse(*args):
        raise AssertionError('read-only run must not execute')
    monkeypatch.setattr(pair,'LighterTrader',Trader)
    monkeypatch.setattr(pair,'preflight',preflight)
    monkeypatch.setattr(pair,'execute',refuse)
    assert (await pair.run(args))['read_only']
    assert not any(tmp_path.iterdir())


async def test_constructor_failure_releases_only_own_lock(tmp_path,monkeypatch):
    cfg,secrets,args=setup_run(tmp_path,monkeypatch,execute=True)
    def fail(*args):
        raise RuntimeError('constructor failed')
    monkeypatch.setattr(pair,'LighterTrader',fail)
    with pytest.raises(RuntimeError,match='constructor'):
        await pair.run(args)
    assert not pair.paths(cfg)[0].with_suffix('.lock').exists()


async def test_existing_trial_is_never_overwritten_for_new_entry(tmp_path,monkeypatch):
    cfg,secrets,args=setup_run(tmp_path,monkeypatch,execute=True)
    path,db=pair.paths(cfg)
    original={'scope_hash':pair.scope(secrets),'phase':'ondo_entry_pending'}
    pair.lighter.save(path,original,first=True)
    class Trader:
        def __init__(self,*args):
            pass
        async def connect(self):
            pass
        async def close(self):
            pass
    monkeypatch.setattr(pair,'LighterTrader',Trader)
    with pytest.raises(ValueError,match='기존 시험 보존'):
        await pair.run(args)
    assert json.loads(path.read_text())==original and not db.exists()


async def test_engine_refuses_unverified_pair_before_market_requests(tmp_path):
    from pathlib import Path
    from olbot.config import load_config, Secrets
    from olbot.engine import Engine
    cfg=load_config(Path(__file__).parent.parent/'config.live-500.example.yaml')
    cfg.storage.sqlite_path=str(tmp_path/'olbot.sqlite')
    pair.lighter.save(pair.paths(cfg)[0],{'phase':'ondo_entry_pending'},first=True)
    ledger=Ledger(':memory:')
    try:
        with pytest.raises(RuntimeError,match='체결/헷지 시험'):
            await Engine(cfg,Secrets(),True,ledger).setup()
    finally:
        ledger.close()
