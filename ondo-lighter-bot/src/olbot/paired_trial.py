"""현재 잔고용 LIT 단발 체결/헷지/정리 시험. 기본 조회, 실행은 별도 승인 필요.

진입은 거래소별 계획 명목가 최대 15 USD, 진입/정리 각 1건으로 총 4건 이하다.
production Hedger를 retries=1로 사용하고, 미확정이면 새 주문을 보내지 않는다.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
import hashlib
import io
import json
import logging
from pathlib import Path
import sqlite3
import time
import uuid

from olbot.config import load_config, load_secrets
from olbot.hedger import Hedger
from olbot.ledger import Ledger, pending_scope
from olbot.lighter.client import LighterTrader, LighterMarket, fetch_market_meta
from olbot import ondo_trial as ondo
from olbot import trial as lighter


def paths(cfg):
    root=Path(cfg.storage.sqlite_path).resolve().parent
    return root/'paired-hedge-trial.json', root/'paired-hedge-trial.sqlite'


def require_resolved_trial(cfg):
    path,db=paths(cfg)
    if db.exists() and not path.exists():
        raise RuntimeError('체결 시험 DB의 기록 파일이 없다. 삭제/교체하지 말고 확인해야 한다')
    if path.exists():
        state=json.loads(path.read_text(encoding='utf-8'))
        if state.get('phase')!='resolved' or state.get('hedge_verified') is not True:
            raise RuntimeError('실제 체결/헷지 시험이 미확정 또는 미검증이다. paired-hedge-trial 기록을 보존한다')
    if db.exists():
        with sqlite3.connect('file:'+db.as_posix()+'?mode=ro',uri=True) as connection:
            if connection.execute('SELECT 1 FROM pending_hedges LIMIT 1').fetchone():
                raise RuntimeError('체결 시험 DB에 미확정 헷지가 남아 있다')


def scope(secrets):
    return hashlib.sha256((secrets.ondo_api_key_id+':'+secrets.lighter_account_index+':'+secrets.lighter_api_key_index).encode()).hexdigest()


def has_pending(path,sql_scope):
    path=Path(path).resolve()
    if not path.exists():
        return False
    with sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='pending_hedges'").fetchone():
            return False
        return bool(db.execute('SELECT 1 FROM pending_hedges WHERE scope=? LIMIT 1',
            (sql_scope,)).fetchone())


def production_has_pending(cfg,secrets):
    return has_pending(cfg.storage.sqlite_path,pending_scope(lighter.BASE,int(secrets.lighter_account_index)))


async def lighter_book(market_id):
    book=await lighter.get(f'/api/v1/orderBookOrders?market_id={market_id}&limit=10')
    bid=max(Decimal(r['price']) for r in book['bids'] if Decimal(r['remaining_base_amount'])>0)
    ask=min(Decimal(r['price']) for r in book['asks'] if Decimal(r['remaining_base_amount'])>0)
    if not 0<bid<ask:
        raise ValueError('Lighter 호가 오류')
    return bid,ask


def calculate_plan(ondo_plan,market,ondo_bid,ondo_ask,lighter_bid,lighter_ask):
    step=ondo.positive_decimal(ondo_plan['base_increment'])
    tick=ondo.positive_decimal(ondo_plan['quote_increment'])
    values=[ondo.positive_decimal(v) for v in (ondo_bid,ondo_ask,lighter_bid,lighter_ask,market.min_base,market.min_quote)]
    ondo_bid,ondo_ask,lighter_bid,lighter_ask=values[:4]
    if ondo_bid>=ondo_ask or lighter_bid>=lighter_ask:
        raise ValueError('호가 순서 오류')
    if abs(((lighter_bid+lighter_ask)/(ondo_bid+ondo_ask))-1)>Decimal('.01'):
        raise ValueError('두 거래소 가격 차이가 1%를 넘음')
    mid=(lighter_bid+lighter_ask)/2
    size=(market.min_hedge_size(mid)/step).to_integral_value(rounding=ROUND_CEILING)*step
    price=(ondo_ask*Decimal('1.002')/tick).to_integral_value(rounding=ROUND_CEILING)*tick
    if size<=0 or size*price>15 or size*lighter_ask*Decimal('1.002')>15:
        raise ValueError('양쪽 최소 주문을 15 USD 안에 맞출 수 없음')
    exact=Decimal(market.to_int_size(size)).scaleb(-market.size_decimals)
    if exact!=size:
        raise ValueError('양쪽 수량 단위가 맞지 않음')
    return {'size':str(size),'ondo_price_cap':str(price),'ondo_notional_cap_usd':str(size*price),
            'lighter_reference_notional_usd':str(size*lighter_ask),'ondo_tick':str(tick),
            'lighter_mid':str(mid),'lighter_market':{'base':'LIT','market_id':market.market_id,
                'size_decimals':market.size_decimals,'price_decimals':market.price_decimals,
                'min_base':str(market.min_base),'min_quote':str(market.min_quote)}}


def market_for(plan):
    data=plan['lighter_market']
    return LighterMarket(data['base'],data['market_id'],data['size_decimals'],data['price_decimals'],
                         Decimal(data['min_base']),Decimal(data['min_quote']))


async def preflight(rest,trader,cfg,secrets):
    if production_has_pending(cfg,secrets):
        raise ValueError('기존 봇 DB에 미확정 헷지가 있음')
    op=await ondo.preflight(rest)
    lp=await lighter.plan_for(trader)
    active=await lighter.get(f'/api/v1/accountActiveOrders?account_index={trader.account_index}',trader._auth())
    if active['orders']:
        raise ValueError('Lighter 기존 주문이 있음')
    market=await fetch_market_meta(lighter.BASE,'LIT',lp['market_id'])
    ob,oa=await ondo.quote()
    lb,la=await lighter_book(market.market_id)
    plan=calculate_plan(op,market,ob,oa,lb,la)
    plan['ready']=all(op[k] for k in ('sufficient_margin','no_positions','no_active_orders')) and lp['sufficient_margin'] and lp['no_existing_positions']
    return plan


async def wait_ondo(rest,state,*,closing=False,timeout=30):
    key='ondo_close_id' if closing else 'ondo_entry_id'
    amount=state['ondo_filled'] if closing else state['plan']['size']
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            raw=await rest.request('GET',ondo.ORDER_PATH+state[key])
            order=ondo.parse_order(raw,state[key],amount,'sell' if closing else 'buy',cleanup=closing,time_in_force='IOC')
            if order['status'] in ondo.TERMINAL:
                actual=await ondo.positions(rest)
                expected=Decimal(amount)-Decimal(order['filled']) if closing else Decimal(order['filled'])
                if actual.get(ondo.MARKET,Decimal(0))==expected and not any(q for m,q in actual.items() if m!=ondo.MARKET):
                    return order
        except Exception:
            pass
        await asyncio.sleep(2)
    return None


class RecordedTrader:
    def __init__(self,trader,state,path):
        self.trader,self.state,self.path=trader,state,path
    def __getattr__(self,name):
        return getattr(self.trader,name)
    async def market_order(self,*args,**kwargs):
        if self.state.get('lighter_entry_attempted'):
            raise RuntimeError('시험 헷지는 1회만 전송 가능')
        market,size,is_sell,worst=args
        if (market.base!='LIT' or not is_sell or size>Decimal(self.state['ondo_filled']) or
                size*worst>15 or size<=0 or worst<=0):
            raise ValueError('헷지 승인 범위 초과')
        self.state['lighter_entry_attempted']=True
        lighter.save(self.path,self.state)
        return await asyncio.wait_for(self.trader.market_order(*args,**kwargs),timeout=15)


async def finish(rest,trader,state,path):
    if not state.get('entries_confirmed'):
        return {'resolved':False,'phase':state['phase']}
    if has_pending(path.with_suffix('.sqlite'),pending_scope(lighter.BASE,trader.account_index)):
        return {'resolved':False,'phase':state['phase']}
    plan=state['plan']
    om=Decimal(state['ondo_filled'])
    lm=Decimal(state.get('lighter_filled','0'))
    market=market_for(plan)
    if om:
        if 'ondo_close_id' not in state:
            current=await ondo.positions(rest)
            if current.get(ondo.MARKET,Decimal(0))!=om:
                return {'resolved':False,'phase':state['phase']}
            bid,_=await ondo.quote()
            tick=Decimal(plan['ondo_tick'])
            price=(bid*Decimal('.998')/tick).to_integral_value(rounding=ROUND_DOWN)*tick
            state.update(phase='ondo_close_pending',ondo_close_id=state['ondo_entry_id']+'_close')
            lighter.save(path,state)
            try:
                await rest.request('POST','/v1/perps/orders',payload={'market':ondo.MARKET,'side':'sell',
                    'type':'limit','timeInForce':'IOC','size':str(om),'price':str(price),'postOnly':False,
                    'reduceOnly':True,'clientOrderId':state['ondo_close_id']})
            except Exception:
                pass
        closed=await wait_ondo(rest,state,closing=True)
        if closed is None or Decimal(closed['filled'])!=om:
            return {'resolved':False,'phase':state['phase']}
    if lm:
        if 'lighter_close_id' not in state:
            pos=lighter.signed_positions(await trader.account())
            if pos.get(market.market_id,Decimal(0))!=-lm:
                return {'resolved':False,'phase':state['phase']}
            _,ask=await lighter_book(market.market_id)
            amount=lm*(Decimal(10)**market.size_decimals)
            if amount!=amount.to_integral_value():
                raise ValueError('정리 수량 단위 불일치')
            scale=Decimal(10)**market.price_decimals
            price=int((ask*Decimal('1.002')*scale).to_integral_value(rounding=ROUND_CEILING))
            state.update(phase='lighter_close_pending',lighter_close_id=trader.new_client_order_index())
            lighter.save(path,state)
            try:
                await asyncio.wait_for(trader.client.create_market_order(market_index=market.market_id,client_order_index=state['lighter_close_id'],
                    base_amount=int(amount),avg_execution_price=price,is_ask=False,reduce_only=True),timeout=15)
            except Exception:
                pass
        closed=await lighter.final_order(trader,{'market_id':market.market_id,'size':str(lm),'position_before':str(-lm)},
                                        state['lighter_close_id'],False)
        if closed is None or closed.filled!=lm:
            return {'resolved':False,'phase':state['phase']}
    if any((await ondo.positions(rest)).values()) or any(lighter.signed_positions(await trader.account()).values()):
        return {'resolved':False,'phase':state['phase']}
    if await rest.request('GET','/v1/perps/orders',params={'status':'open','limit':1}):
        return {'resolved':False,'phase':state['phase']}
    if (await lighter.get(f'/api/v1/accountActiveOrders?account_index={trader.account_index}',trader._auth()))['orders']:
        return {'resolved':False,'phase':state['phase']}
    state['phase']='resolved'
    lighter.save(path,state)
    return {'resolved':True,'hedge_verified':state.get('hedge_verified',False),
            'ondo_filled':str(om),'lighter_filled':str(lm),'positions_zero':True,'no_active_orders':True}


async def execute(rest,trader,state,path,ledger):
    plan=state['plan']
    if not plan['ready']:
        raise ValueError('증거금/기존 주문/포지션 조건 미충족')
    state['phase']='ondo_entry_pending'
    lighter.save(path,state)
    try:
        await rest.request('POST','/v1/perps/orders',payload={'market':ondo.MARKET,'side':'buy','type':'limit',
            'timeInForce':'IOC','size':plan['size'],'price':plan['ondo_price_cap'],'postOnly':False,'reduceOnly':False,
            'clientOrderId':state['ondo_entry_id']})
    except Exception:
        pass
    order=await wait_ondo(rest,state)
    if order is None:
        return {'resolved':False,'phase':state['phase']}
    state['ondo_filled']=order['filled']
    lighter.save(path,state)
    filled=Decimal(order['filled'])
    market=market_for(plan)
    lb,la=await lighter_book(market.market_id)
    mid=(lb+la)/2
    sql_scope=pending_scope(lighter.BASE,trader.account_index)
    hedger=None
    if filled>=market.min_hedge_size(mid) and filled*la*Decimal('1.002')<=15:
        def pending(base,value):
            if value is not None:
                if state.get('lighter_entry_id') is not None:
                    raise RuntimeError('두 번째 헷지 전송 거부')
                state.update(lighter_entry_id=value.client_order_index,lighter_size=str(value.size))
                lighter.save(path,state)
            ledger.save_pending_hedge(sql_scope,base,value)
        hedger=Hedger(RecordedTrader(trader,state,path),{'LIT':market},15,1,
                      record=ledger.record_hedge,save_pending=pending)
        hedger.set_ondo_positions({'LIT':filled})
        await hedger.refresh_lighter()
        await hedger.hedge('LIT',mid)
    if state.get('lighter_entry_attempted'):
        result=await lighter.final_order(trader,{'market_id':market.market_id,'size':state['lighter_size']},
                                        state['lighter_entry_id'],True)
        if result is None:
            return {'resolved':False,'phase':state['phase']}
        state['lighter_filled']=str(result.filled)
        for _ in range(15):
            if await hedger._resolve_pending('LIT') is None:
                break
            await asyncio.sleep(2)
        else:
            return {'resolved':False,'phase':state['phase']}
    elif hedger is not None and hedger.states['LIT'].pending is not None:
        return {'resolved':False,'phase':state['phase']}
    else:
        state['lighter_filled']='0'
    if ledger.load_pending_hedges(sql_scope):
        return {'resolved':False,'phase':state['phase']}
    state['hedge_verified']=filled>0 and filled==Decimal(state['lighter_filled'])
    state['entries_confirmed']=True
    lighter.save(path,state)
    return await finish(rest,trader,state,path)


async def run(args):
    cfg,secrets=load_config(args.config),load_secrets(args.env)
    if cfg.ondo.rest_url.rstrip('/')!=ondo.BASE or cfg.lighter.base_url.rstrip('/')!=lighter.BASE:
        raise ValueError('메인넷 주소 불일치')
    ondo.require_resolved_trial(cfg)
    lighter.require_resolved_trial(cfg)
    lighter_scope=pending_scope(lighter.BASE,int(secrets.lighter_account_index))
    path,db=paths(cfg)
    mutating=args.execute or args.finish_known
    if mutating and (args.approve_max_position_usd!='15' or not args.approve_cleanup or (args.execute and args.finish_known)):
        raise ValueError('거래소별 15 USD 진입 및 양쪽 정리 승인 플래그가 필요하다')
    lock=path.with_suffix('.lock')
    if mutating:
        lock.parent.mkdir(parents=True,exist_ok=True)
        with lock.open('x',encoding='utf-8') as file:
            file.write('exclusive paired trial\n')
    trader=None
    ledger=None
    try:
        trader=LighterTrader(lighter.BASE,int(secrets.lighter_account_index),int(secrets.lighter_api_key_index),secrets.lighter_api_key_private_key)
        await trader.connect()
        rest=ondo.make_rest(secrets)
        if path.exists():
            state=json.loads(path.read_text(encoding='utf-8'))
            if args.execute or state['scope_hash']!=scope(secrets):
                raise ValueError('기존 시험 보존 필요. 새 진입을 보내지 않는다')
            if args.finish_known:
                return await finish(rest,trader,state,path)
            return {'read_only':True,'phase':state['phase'],'hedge_verified':state.get('hedge_verified',False)}
        if args.finish_known or db.exists():
            raise ValueError('기존 시험 기록 확인 필요')
        plan=await preflight(rest,trader,cfg,secrets)
        if not args.execute:
            return {'read_only':True,'plan':plan}
        state={'phase':'prepared','scope_hash':scope(secrets),'plan':plan,
               'ondo_entry_id':'olbot_pair_'+uuid.uuid4().hex,'lighter_entry_attempted':False}
        lighter.save(path,state,first=True)
        ledger=Ledger(db)
        if ledger.load_pending_hedges(lighter_scope):
            raise ValueError('시험 DB에 미확정 헷지가 있음')
        return await execute(rest,trader,state,path,ledger)
    finally:
        if ledger is not None:
            ledger.close()
        try:
            if trader is not None:
                await trader.close()
        finally:
            if mutating:
                lock.unlink()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='config.yaml')
    parser.add_argument('--env',default='.env')
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--finish-known',action='store_true')
    parser.add_argument('--approve-max-position-usd')
    parser.add_argument('--approve-cleanup',action='store_true')
    args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        try:
            result=asyncio.run(run(args))
        except Exception as error:
            result={'failed':True,'error_type':type(error).__name__}
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result.get('read_only') or (result.get('resolved') and result.get('hedge_verified')) else 1


if __name__=='__main__':
    raise SystemExit(main())
