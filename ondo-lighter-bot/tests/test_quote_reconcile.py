"""열린 주문 대사는 모의 REST로만 검사한다. 실제 주문은 보내지 않는다."""
import asyncio
import copy
import json
from decimal import Decimal as D
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from olbot.config import Secrets, load_config
from olbot.engine import Engine, UNKNOWN_QUOTES
from olbot.ledger import Ledger
from olbot.ondo.rest import OndoRest, OpenOrder, parse_open_orders
from olbot.quoter import TargetQuotes, target_quotes
from olbot.lighter.client import LighterMarket
from olbot import ratelimit


ROOT=Path(__file__).parent.parent


class FakeOndo:
    def __init__(self):
        self.orders={}
        self.reads=0
        self.sent=[]
        self.serial=0
        self.cancel_error=False

    async def open_orders(self):
        self.reads+=1
        return dict(self.orders)

    async def cancel_all(self,market=None):
        self.sent.append(('cancel',market))
        if self.cancel_error:
            raise OSError('timeout')
        self.orders={k:v for k,v in self.orders.items() if market is not None and v.market!=market}

    async def create_batch(self,payload):
        self.sent.append(('create',payload))
        added=[]
        for row in payload:
            self.serial+=1
            oid=f'order-{self.serial}'
            self.orders[oid]=OpenOrder(oid,row['market'],row['side'])
            added.append({**row,'orderId':oid})
        return added,[]


@pytest.fixture
def engine():
    cfg=load_config(ROOT/'config.live-500.example.yaml')
    cfg.markets=cfg.markets[:1]
    ledger=Ledger(':memory:')
    eng=Engine(cfg,Secrets(),True,ledger)
    eng.ondo_rest=FakeOndo()
    eng.ondo_meta={'LIT':{'market':'LIT-USD.P','quoteIncrement':'.0001','baseIncrement':'1'}}
    eng.lighter_markets={'LIT':LighterMarket('LIT',120,2,4,D('1'),D('10'))}
    eng.board.update_ondo('LIT',D('2.999'),D('3.001'),1000)
    eng.market_block_reason=lambda b,now:None
    yield eng
    ledger.close()


def target(engine):
    q,m=engine.cfg.quoting,engine.mcfg['LIT']
    return target_quotes(D(3),q.offset_bps,m.size_usd_per_side,D(0),m.max_inventory_usd,
                         q.skew_bps_per_1k,D('.0001'),D(1),engine.lighter_markets['LIT'].min_hedge_size(D(3)))


@pytest.mark.parametrize('missing',['buy','sell'])
async def test_missing_order_is_removed_then_same_price_is_requoted(engine,caplog,missing):
    await engine.place('LIT',target(engine))
    original=engine.current['LIT']
    buy,sell=engine.order_ids['LIT']['buy'],engine.order_ids['LIT']['sell']
    del engine.ondo_rest.orders[engine.order_ids['LIT'][missing]]
    with caplog.at_level(logging.WARNING):
        await engine.reconcile_orders()
    assert '호가 대사 불일치' in caplog.text
    assert engine.current['LIT']==(TargetQuotes(None,original.ask) if missing=='buy' else TargetQuotes(original.bid,None))
    assert engine.order_ids['LIT']==({'sell':sell} if missing=='buy' else {'buy':buy}) and 'LIT' in engine.dirty
    assert len(engine.ondo_rest.sent)==2  # 대사 함수는 조회만 한다.
    await engine._quote_market('LIT',1001)
    assert engine.current['LIT']==original
    assert set(engine.order_ids['LIT'].values()).isdisjoint({buy,sell})
    assert [kind for kind,_ in engine.ondo_rest.sent]==['cancel','create','cancel','create']


async def test_empty_snapshot_removes_both_legs(engine):
    await engine.place('LIT',target(engine))
    engine.ondo_rest.orders.clear()
    await engine.reconcile_orders()
    assert engine.current['LIT'] is None and engine.order_ids['LIT']=={}
    assert 'LIT' in engine.dirty


async def test_matching_snapshot_preserves_tracking_without_requote_or_warning(engine,caplog):
    await engine.place('LIT',target(engine))
    original=engine.current['LIT']
    with caplog.at_level(logging.INFO):
        await engine.reconcile_orders()
    await engine._quote_market('LIT',1001)
    assert engine.current['LIT'] is original and not engine.dirty
    assert len(engine.ondo_rest.sent)==2 and engine.ondo_rest.reads==1
    assert '호가 대사 불일치' not in caplog.text
    assert '호가 대사 확인: LIT tracked=2 open=2 exchange_total=2' in caplog.text


async def test_confirmation_excludes_missing_or_untracked_order_from_bot_open_count(engine,caplog):
    await engine.place('LIT',target(engine))
    del engine.ondo_rest.orders[engine.order_ids['LIT']['buy']]
    engine.ondo_rest.orders['manual']=OpenOrder('manual','LIT-USD.P','buy')
    with caplog.at_level(logging.INFO):
        await engine.reconcile_orders()
    assert '호가 대사 확인: LIT tracked=2 open=1 exchange_total=2' in caplog.text
    rows=engine.ledger.db.execute("SELECT detail FROM events WHERE kind='quote_reconciled'").fetchall()
    assert rows==[('LIT tracked=2 open=1 exchange_total=2',)]


async def test_untracked_exchange_order_is_logged_but_not_adopted_or_canceled(engine,caplog):
    engine.ondo_rest.orders['manual']=OpenOrder('manual','LIT-USD.P','buy')
    with caplog.at_level(logging.WARNING):
        await engine.reconcile_orders()
    assert '호가 대사 불일치' in caplog.text and '미추적 주문=1' in caplog.text
    assert engine.current['LIT'] is None and not engine.order_ids['LIT']
    assert not engine.ondo_rest.sent and 'manual' in engine.ondo_rest.orders


async def test_order_in_unmanaged_market_is_also_logged_without_mutation(engine,caplog):
    engine.ondo_rest.orders['other']=OpenOrder('other','OTHER-USD.P','sell')
    with caplog.at_level(logging.WARNING):
        await engine.reconcile_orders()
    assert '호가 대사 불일치 OTHER-USD.P' in caplog.text
    assert not engine.ondo_rest.sent and not engine.dirty


async def test_pause_after_create_cancels_without_reentering_quote_lock(engine):
    create=engine.ondo_rest.create_batch
    async def pause_after_create(payload):
        result=await create(payload)
        engine.paused='test pause'
        return result
    engine.ondo_rest.create_batch=pause_after_create
    await asyncio.wait_for(engine.place('LIT',target(engine)),timeout=1)
    assert engine.current['LIT'] is None and not engine.order_ids['LIT']
    assert [kind for kind,_ in engine.ondo_rest.sent]==['cancel','create','cancel']


async def test_unknown_submission_is_not_cleared_by_absence(engine):
    engine.current['LIT']=UNKNOWN_QUOTES
    engine.order_ids['LIT']={'buy':'possibly-open'}
    await engine.reconcile_orders()
    assert engine.current['LIT'] is UNKNOWN_QUOTES
    assert engine.order_ids['LIT']=={'buy':'possibly-open'}


async def test_failed_cancel_keeps_ids_until_confirmed_success(engine):
    await engine.place('LIT',target(engine))
    ids=dict(engine.order_ids['LIT'])
    engine.ondo_rest.cancel_error=True
    assert not await engine.pull_market('LIT','test')
    assert engine.order_ids['LIT']==ids
    engine.ondo_rest.cancel_error=False
    assert await engine.cancel_all_quotes()
    assert not engine.order_ids['LIT'] and engine.current['LIT'] is None


async def test_missing_create_identity_remains_unknown_for_cancel_first(engine):
    async def bad_create(payload):
        return payload,[]  # orderId가 없는 응답
    engine.ondo_rest.create_batch=bad_create
    await engine.place('LIT',target(engine))
    assert engine.current['LIT'] is UNKNOWN_QUOTES and not engine.order_ids['LIT']


async def test_snapshot_and_quote_replacement_are_serialized(engine):
    await engine.place('LIT',target(engine))
    entered,release=asyncio.Event(),asyncio.Event()
    async def delayed():
        result=dict(engine.ondo_rest.orders)
        entered.set()
        await release.wait()
        return result
    engine.ondo_rest.open_orders=delayed
    reconciliation=asyncio.create_task(engine.reconcile_orders())
    await entered.wait()
    replacement=asyncio.create_task(engine.place('LIT',target(engine)))
    await asyncio.sleep(0)
    assert len(engine.ondo_rest.sent)==2
    release.set()
    await asyncio.gather(reconciliation,replacement)
    assert set(engine.order_ids['LIT'].values())==set(engine.ondo_rest.orders)
    assert engine.current['LIT']==target(engine) and not engine.dirty


@pytest.mark.parametrize('block',['killed','paused'])
async def test_missing_order_does_not_bypass_risk_stop(engine,block):
    await engine.place('LIT',target(engine))
    engine.ondo_rest.orders.clear()
    await engine.reconcile_orders()
    setattr(engine,block,'test stop')
    await engine._quote_market('LIT',1001)
    assert len(engine.ondo_rest.sent)==2 and engine.current['LIT'] is None


async def test_dry_run_never_queries_account_orders(engine):
    engine.live=False
    await engine.reconcile_orders()
    assert engine.ondo_rest.reads==0


def test_actual_empty_orders_response_is_a_complete_snapshot():
    response=json.loads((ROOT/'tests/fixtures/ondo-open-orders-empty.json').read_text())
    assert parse_open_orders(response)=={}


async def test_main_run_starts_and_stops_reconciliation_task(engine,monkeypatch):
    started=[]
    async def no_op(*args,**kwargs):
        pass
    async def background():
        await engine.stop.wait()
    async def reconciliation():
        started.append(True)
        engine.stop.set()
    engine.ondo_ws=SimpleNamespace(run=background,stop=lambda:None)
    engine.lighter_ws=SimpleNamespace(run=background,stop=lambda:None)
    engine.private_ws=SimpleNamespace(run=background,stop=lambda:None)
    for name in ('setup','live_startup','_wait_for_quotes','_wait_for_login'):
        monkeypatch.setattr(engine,name,no_op)
    for name in ('hedge_loop','quote_loop','risk_loop','record_loop'):
        monkeypatch.setattr(engine,name,background)
    monkeypatch.setattr(engine,'order_reconcile_loop',reconciliation)
    assert await asyncio.wait_for(engine.run(None),timeout=2)==0
    assert started==[True] and engine.stop.is_set()


async def test_loop_retries_only_after_60_seconds_and_keeps_tracking_on_failure(engine,monkeypatch,caplog):
    await engine.place('LIT',target(engine))
    original=engine.current['LIT']
    sleeps=[]
    clock=[0.0]
    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0]+=seconds
        if len(sleeps)==3:
            engine.stop.set()
    calls=[]
    async def flaky():
        calls.append(1)
        clock[0]+=5  # 조회가 5초 걸려도 다음 시작은 60초 간격이다.
        assert engine.current['LIT']==original
        if len(calls)==1:
            raise OSError('query failed')
    engine.ondo_rest.open_orders=flaky
    # 두 번째는 정상 스냅샷을 돌려준다.
    async def read():
        await flaky()
        return dict(engine.ondo_rest.orders)
    engine.ondo_rest.open_orders=read
    monkeypatch.setattr('olbot.engine.asyncio.sleep',sleep)
    monkeypatch.setattr('olbot.engine.time',SimpleNamespace(monotonic=lambda:clock[0]))
    with caplog.at_level(logging.WARNING):
        await engine.order_reconcile_loop()
    assert sleeps==[60,55,55] and len(calls)==2
    assert '호가 대사 조회 실패' in caplog.text and engine.current['LIT']==original


def raw(oid='buy-id',side='buy'):
    return {'orderId':oid,'market':'LIT-USD.P','side':side,'status':'open'}


@pytest.mark.parametrize('response',[
    {'success':True,'result':None},
    {'success':True,'result':[],'pageInfo':{'nextCursor':'next'}},
    {'success':True,'result':[],'pageInfo':{'nextCursor':0}},
    {'success':True,'result':[],'pageInfo':[]},
    {'success':True,'result':[raw(),raw()]},
    {'success':True,'result':[{**raw(),'status':'canceled'}]},
    {'success':True,'result':[{**raw(),'orderId':''}]},
    {'success':True,'result':[{**raw(),'side':'unknown'}]},
    {'success':True,'result':[{'market':'LIT-USD.P','side':'buy','status':'open'}]},
    {'success':False,'result':[]},
])
async def test_invalid_or_truncated_snapshot_cannot_drop_tracking(engine,monkeypatch,response):
    await engine.place('LIT',target(engine))
    before=engine.current['LIT'],copy.deepcopy(engine.order_ids)
    rest=OndoRest('https://not-used','dummy','dummy',engine.bucket)
    async def request(*args,**kwargs):
        return response
    monkeypatch.setattr(rest,'_request_response',request)
    engine.ondo_rest=rest
    with pytest.raises((ValueError,KeyError)):
        await engine.reconcile_orders()
    assert (engine.current['LIT'],engine.order_ids)==before and not engine.dirty


def test_full_limit_snapshot_is_not_treated_as_complete():
    with pytest.raises(ValueError):
        parse_open_orders({'success':True,'result':[raw(str(i)) for i in range(1000)]})


async def test_order_identity_conflict_preserves_entire_snapshot(engine):
    await engine.place('LIT',target(engine))
    oid=engine.order_ids['LIT']['buy']
    engine.ondo_rest.orders[oid]=OpenOrder(oid,'OTHER-USD.P','buy')
    before=engine.current['LIT'],copy.deepcopy(engine.order_ids)
    with pytest.raises(ValueError):
        await engine.reconcile_orders()
    assert (engine.current['LIT'],engine.order_ids)==before


async def test_all_ondo_requests_share_one_per_second_bucket(engine,monkeypatch):
    clock=[0.0]
    async def sleep(seconds):
        clock[0]+=seconds
    monkeypatch.setattr(ratelimit,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    monkeypatch.setattr(ratelimit.asyncio,'sleep',sleep)
    cfg=engine.cfg
    cfg.ondo.rest_rate_per_sec=10  # 수동 설정이 커도 이 봇은 초당 1회로 제한한다.
    ledger=Ledger(':memory:')
    checked=Engine(cfg,Secrets(),True,ledger)
    rest=OndoRest('https://not-used','dummy','dummy',checked.bucket)
    calls=[]
    def fake(method,path,body):
        calls.append((clock[0],method,path))
        result=[] if path.startswith('/v1/perps/orders?') else {}
        return 200,{'success':True,'result':result}
    monkeypatch.setattr(rest,'_do',fake)
    checked.ondo_rest=rest
    checked.ondo_meta=engine.ondo_meta
    try:
        await checked.reconcile_orders()
        await rest.balance()
        await rest.positions()
        await rest.cancel_all()  # _do는 모의 함수라 실제 전송은 없다.
        assert [t for t,_,_ in calls]==[0,1,2,3]
        assert calls[0][2]=='/v1/perps/orders?status=open&limit=1000'
    finally:
        ledger.close()
