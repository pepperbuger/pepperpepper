"""승인된 잔여 LIT/ENA만 정리. 기본 조회, --execute일 때만 최대 네 reduce-only IOC.

응답 유실·부분 체결 시 재전송하지 않는다. 새 저널은 전송 전에 fsync한다.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR
import hashlib
import io
import json
import logging
from pathlib import Path
import time
import uuid

from olbot.config import Secrets, load_config
from olbot.lighter.client import LighterTrader, parse_order_status
from olbot.ondo.rest import OndoRest, OndoError
from olbot.paper import vwap
from olbot.ratelimit import TokenBucket
from olbot import paired_trial, trial

SCOPE = {'LIT': D(40), 'ENA': D(-453)}  # Ondo signed positions; Lighter is the opposite
MAX_COST = D(2)


def number(value):
    d = D(str(value))
    if not d.is_finite(): raise ValueError('nonfinite response')
    return d


def nonzero(positions): return {b:number(q) for b,q in positions.items() if number(q)}


def make_leg(base, venue, position, bids, asks, tick, step, min_base, min_quote):
    if base not in SCOPE or venue not in ('ondo','lighter'): raise ValueError('outside approved scope')
    position, tick, step = number(position), number(tick), number(step)
    if tick <= 0 or step <= 0 or position == 0: raise ValueError('invalid close size/increment')
    if abs(position) > abs(SCOPE[base]): raise ValueError('size above approval')
    expected_sign = SCOPE[base] if venue == 'ondo' else -SCOPE[base]
    if position * expected_sign <= 0: raise ValueError('not reducing approved position')
    size = abs(position)
    if size % step or size < min_base: raise ValueError('size/minimum mismatch')
    bids = sorted([(number(p),number(q)) for p,q in bids if number(q)>0], reverse=True)
    asks = sorted([(number(p),number(q)) for p,q in asks if number(q)>0])
    if not bids or not asks or not 0 < bids[0][0] < asks[0][0]: raise ValueError('invalid order book')
    side = 'sell' if position > 0 else 'buy'
    levels = bids if side == 'sell' else asks
    best = levels[0][0]
    raw_limit = best * (D('.998') if side == 'sell' else D('1.002'))
    limit = (raw_limit/tick).to_integral_value(rounding=ROUND_CEILING if side=='sell' else ROUND_FLOOR)*tick
    within = [(p,q) for p,q in levels if (p>=limit if side=='sell' else p<=limit)]
    execution = vwap(within,size)
    if execution is None: raise ValueError('insufficient depth inside 20bps protection')
    if max(size*best,size*execution,size*limit)>200 or size*execution<min_quote: raise ValueError('notional/minimum outside approval')
    mid=(bids[0][0]+asks[0][0])/2
    fee = D('.00025') if venue=='ondo' else D(0)
    sign = D(1) if side=='sell' else D(-1)
    return {'base':base,'venue':venue,'side':side,'size':str(size),'position_before':str(position),
            'price_limit':str(limit),'mid':str(mid),'expected_vwap':str(execution),
            'expected_cost':str(sign*size*(mid-execution)+size*execution*fee),
            'worst_cost':str(sign*size*(mid-limit)+size*limit*fee),'tick':str(tick)}


def parse_ondo_close(raw, leg, cid, market):
    if (raw['clientOrderId']!=cid or raw['market']!=market or raw['side']!=leg['side']
            or raw['type']!='limit' or raw['timeInForce']!='IOC' or raw.get('reduceOnly') is not True
            or number(raw['size'])!=number(leg['size']) or number(raw['price'])!=number(leg['price_limit'])):
        raise ValueError('Ondo order identity/flags mismatch')
    filled, cost, fee = number(raw['filledSize']),number(raw['filledCost']),number(raw['fee'])
    if not 0<=filled<=number(leg['size']) or cost<0 or (filled>0 and cost<=0): raise ValueError('Ondo quantities invalid')
    if raw['status'] not in {'open','pending','untriggered','canceled','fullyfilled'}: raise ValueError('Ondo unknown status')
    if raw['status']=='fullyfilled' and filled!=number(leg['size']): raise ValueError('Ondo inconsistent filled')
    rebate=number(raw.get('feeRebate',0))
    return {'terminal':raw['status'] in {'canceled','fullyfilled'},'status':raw['status'],
            'filled':str(filled),'quote':str(cost),'fee':str(fee-rebate),'reduce_only':True}


def parse_lighter_close(raw, leg, coi, market_id, account_index):
    if (int(raw['client_order_index'])!=coi or int(raw['market_index'])!=market_id
            or int(raw['owner_account_index'])!=account_index or type(raw['is_ask']) is not bool
            or raw['is_ask']!=(leg['side']=='sell') or raw.get('reduce_only') is not True
            or raw['time_in_force']!='immediate-or-cancel'):
        raise ValueError('Lighter order identity/flags mismatch')
    order=parse_order_status(raw)
    if order.initial!=number(leg['size']): raise ValueError('Lighter size mismatch')
    cost=number(raw['filled_quote_amount'])
    if cost<0 or (order.filled>0 and cost<=0): raise ValueError('Lighter quote invalid')
    return {'terminal':order.terminal,'status':order.status,'filled':str(order.filled),'quote':str(cost),
            'fee':'0','fee_basis':'Standard zero taker fee, non-TWAP','reduce_only':True}


def cost_at_reference(leg, result, mid):
    q,quote=number(result['filled']),number(result['quote'])
    return (q*mid-quote if leg['side']=='sell' else quote-q*mid)+number(result['fee'])


async def execute(adapter, path):
    path=Path(path)
    if path.exists(): raise ValueError('cleanup journal already exists; never resend')
    state={'scope':{b:str(q) for b,q in SCOPE.items()},'scope_hash':adapter.scope_hash,
           'phase':'preflight','attempts':[],'orders_sent':0,'approval_max_orders':4,'approval_cost_budget':'2',
           'started_ts':time.time()}
    trial.save(path,state,first=True)
    try:
        before=await adapter.snapshot()
        state['before']=before
        if nonzero(before['ondo_positions'])!=SCOPE or nonzero(before['lighter_positions'])!={b:-q for b,q in SCOPE.items()}:
            raise ValueError('positions differ from approved exact scope')
        if before['ondo_open'] or before['lighter_open'] or before['pending']: raise ValueError('active or pending orders exist')
        plans={}
        for base,q in SCOPE.items():
            for venue,pos in (('ondo',q),('lighter',-q)):
                plans[(base,venue)]=await adapter.leg(base,venue,pos)
        if sum(number(p['worst_cost']) for p in plans.values())>MAX_COST:
            raise ValueError('initial protected worst cost above 2 USD')
        state['initial_plans']=list(plans.values())
        trial.save(path,state)
        used=D(0)
        remaining=dict(plans)
        for base, initial in SCOPE.items():
            current=await adapter.snapshot()
            if current['ondo_open'] or current['lighter_open'] or current['pending']:
                raise ValueError('active/pending appeared before next market')
            expected_o={b:(D(0) if b=='LIT' and base=='ENA' else q) for b,q in SCOPE.items()}
            expected_l={b:-q for b,q in expected_o.items()}
            if nonzero(current['ondo_positions'])!=nonzero(expected_o) or nonzero(current['lighter_positions'])!=nonzero(expected_l):
                raise ValueError('positions changed before market')
            # Refresh both legs before the first order of a market.
            remaining[(base,'ondo')]=await adapter.leg(base,'ondo',initial)
            remaining[(base,'lighter')]=await adapter.leg(base,'lighter',-initial)
            if used+sum(number(p['worst_cost']) for p in remaining.values())>MAX_COST:
                raise ValueError('refreshed protected cost above remaining budget')
            references={venue:number(remaining[(base,venue)]['mid']) for venue in ('ondo','lighter')}
            size=abs(initial)
            for venue in ('ondo','lighter'):
                position=initial if venue=='ondo' else (-size if initial>0 else size)
                leg=await adapter.leg(base,venue,position)
                if venue=='lighter': leg['position_before']=str(-initial)  # 부분 정리에도 실제 기존 포지션에서 계산
                sign=D(1) if leg['side']=='sell' else D(-1)
                cap_cost=sign*number(leg['size'])*(references[venue]-number(leg['price_limit']))
                cap_cost+=number(leg['size'])*number(leg['price_limit'])*(D('.00025') if venue=='ondo' else 0)
                others=sum(number(p['worst_cost']) for k,p in remaining.items() if k!=(base,venue))
                if used+cap_cost+others>MAX_COST: raise ValueError('price moved above approved total budget')
                if state['orders_sent']>=4: raise ValueError('order count approval exhausted')
                cid=adapter.order_id(venue)
                record={'leg':leg,'client_id':cid,'phase':'send_unknown','sent_ts':time.time()}
                state['attempts'].append(record);state['orders_sent']+=1
                state['phase']=base+'_'+venue+'_pending'
                trial.save(path,state)  # fsync BEFORE the only send
                try:
                    outcome=await adapter.send(leg,cid)
                    record['ack']='rejected' if outcome=='rejected' else 'returned'
                except Exception as exc:
                    record['ack']='unknown:'+type(exc).__name__
                trial.save(path,state)
                if record['ack']=='rejected':
                    state['urgent']=venue=='lighter'
                    raise ValueError('explicit order rejection; stop immediately without resend')
                result=await adapter.confirm(leg,cid,current)
                if result is None:
                    state['urgent']=True
                    raise ValueError('order outcome unknown; no resend and no next market')
                record['result']=result;record['phase']='terminal_position_confirmed'
                used+=cost_at_reference(leg,result,references[venue])
                state['execution_cost_vs_reference']=str(used)
                remaining.pop((base,venue),None)
                trial.save(path,state)
                if venue=='ondo':
                    size=number(result['filled'])
                    if size==0: raise ValueError('Ondo IOC canceled without fill; stop')
                    current['ondo_positions'][base]=str(initial+(-size if initial>0 else size))
                elif number(result['filled'])!=size:
                    state['urgent']=True
                    raise ValueError('one-sided residual after Lighter IOC; notify immediately')
            verified=await adapter.snapshot()
            state[base+'_after']=verified
            trial.save(path,state)
            if (number(verified['ondo_positions'].get(base,0)) or number(verified['lighter_positions'].get(base,0))
                    or verified['ondo_open'] or verified['lighter_open'] or verified['pending']):
                raise ValueError('market not fully flat/clear; do not advance')
            state['phase']=base+'_flat_confirmed';trial.save(path,state)
        state['phase']='all_flat'
    except Exception as exc:
        state['phase']='halted'
        state['error_type']=type(exc).__name__
        state['reason']=str(exc) if isinstance(exc,ValueError) else 'operation failed; inspect journal without resend'
    # Even on a halt, immediately inspect exposure and return; never wait for a scheduled heartbeat.
    try:
        after=await adapter.snapshot()
        state['after']=after
        state['urgent']=state.get('urgent',False) or any(number(after['ondo_positions'].get(b,0))+number(after['lighter_positions'].get(b,0))!=0 for b in SCOPE)
        if state['phase']=='all_flat':
            await asyncio.sleep(1.1)
            second=await adapter.snapshot()
            state['after_second']=second
            if nonzero(second['ondo_positions']) or nonzero(second['lighter_positions']) or second['ondo_open'] or second['lighter_open'] or second['pending']:
                state['phase']='halted';state['urgent']=True;state['reason']='final verification changed'
            else:
                state['phase']='resolved'
        if 'before' in state:
            before_total=number(state['before']['ondo_equity'])+number(state['before']['lighter_equity'])
            end=state.get('after_second',after)
            state['final_balances_stable']=(after['ondo_equity']==end['ondo_equity'] and after['lighter_equity']==end['lighter_equity'])
            state['asset_change_usdc']=str(number(end['ondo_equity'])+number(end['lighter_equity'])-before_total)
    except Exception as exc:
        state['final_snapshot_error']=type(exc).__name__;state['urgent']=bool(state['attempts'])
    state['ended_ts']=time.time()
    trial.save(path,state)
    return state


class Native:
    def __init__(self,cfg,secrets):
        self.cfg,self.secrets=cfg,secrets
        if cfg.ondo.rest_url!='https://api.ondoperps.xyz' or cfg.lighter.base_url!=trial.BASE:
            raise ValueError('unapproved environment')
        self.rest=OndoRest(cfg.ondo.rest_url,secrets.ondo_api_key_id,secrets.ondo_api_secret,TokenBucket(1,1),timeout=10)
        self.trader=LighterTrader(cfg.lighter.base_url,int(secrets.lighter_account_index),int(secrets.lighter_api_key_index),secrets.lighter_api_key_private_key)
        self.scope_hash=paired_trial.scope(secrets)
        self.om,self.lm={},{ }

    async def setup(self):
        from olbot import ondo_trial
        trial.require_resolved_trial(self.cfg)
        ondo_trial.require_resolved_trial(self.cfg)
        paired_trial.require_resolved_trial(self.cfg)
        await self.trader.connect()
        self.om={r['pair']['base']:r for r in (await self.rest.request('GET','/v1/markets'))['perps']['tradingPairs'] if not r.get('disabled')}
        books=(await trial.get('/api/v1/orderBooks'))['order_books']
        for base in SCOPE:
            matches=[r for r in books if r['symbol']==base and r['market_type']=='perp' and r['status']=='active']
            if len(matches)!=1: raise ValueError('market metadata not unique')
            mid=int(matches[0]['market_id'])
            detail=(await trial.get(f'/api/v1/orderBookDetails?market_id={mid}'))['order_book_details'][0]
            self.lm[base]={'id':mid,'size_decimals':int(detail['supported_size_decimals']),
                           'price_decimals':int(detail['supported_price_decimals']),
                           'min_base':number(detail['min_base_amount']),'min_quote':number(detail['min_quote_amount'])}

    async def ondo_positions(self):
        rows=await self.rest.request('GET','/v1/perps/positions')
        if not isinstance(rows,list): raise ValueError('invalid Ondo position list')
        out={}
        for r in rows:
            market=r['market'];q=abs(number(r['netQuantity']))
            if market in out or (q and r['direction'] not in ('long','short')): raise ValueError('invalid Ondo position')
            out[market]=q if r['direction']=='long' else -q
        return {m.split('-')[0]:q for m,q in out.items()}

    async def snapshot(self):
        op=await self.ondo_positions()
        oo=await self.rest.open_orders()
        ob=await self.rest.balance()
        acct=await self.trader.account()
        if int(acct['account_index'])!=self.trader.account_index: raise ValueError('account identity mismatch')
        lp=trial.signed_positions(acct)
        byid={d['id']:b for b,d in self.lm.items()}
        lp={byid.get(k,'unmanaged:'+str(k)):q for k,q in lp.items() if q}
        active=await trial.get(f'/api/v1/accountActiveOrders?account_index={self.trader.account_index}',self.trader._auth())
        return {'ts':time.time(),'ondo_positions':{k:str(v) for k,v in op.items()},'lighter_positions':{k:str(v) for k,v in lp.items()},
                'ondo_open':len(oo),'lighter_open':len(active['orders']),
                'pending':paired_trial.production_has_pending(self.cfg,self.secrets),
                'ondo_equity':str(number(ob['marginBalance'])),'lighter_equity':str(number(acct['total_asset_value']))}

    async def leg(self,base,venue,position):
        if venue=='ondo':
            raw=await self.rest.request('GET','/v1/perps/depth',params={'market':self.om[base]['market'],'depth':100})
            bids,asks=raw['bids'],raw['asks']
            tick,step=number(self.om[base]['quoteIncrement']),number(self.om[base]['baseIncrement'])
            minimum,quote=step,D(0)
        else:
            meta=self.lm[base]
            raw=await trial.get(f"/api/v1/orderBookOrders?market_id={meta['id']}&limit=100")
            bids,asks=[[(r['price'],r['remaining_base_amount']) for r in raw[k]] for k in ('bids','asks')]
            tick,step=D(1).scaleb(-meta['price_decimals']),D(1).scaleb(-meta['size_decimals'])
            minimum,quote=meta['min_base'],meta['min_quote']
        return make_leg(base,venue,position,bids,asks,tick,step,minimum,quote)

    def order_id(self,venue):
        return 'codex_close_'+uuid.uuid4().hex[:20] if venue=='ondo' else self.trader.new_client_order_index()

    async def send(self,leg,cid):
        if leg['venue']=='ondo':
            try:
                await self.rest.request('POST','/v1/perps/orders',payload={'market':self.om[leg['base']]['market'],
                    'side':leg['side'],'type':'limit','timeInForce':'IOC','size':leg['size'],'price':leg['price_limit'],
                    'reduceOnly':True,'postOnly':False,'clientOrderId':cid})
            except OndoError as exc:
                if exc.status in (200,400,401,403): return 'rejected'
                raise
        else:
            m=self.lm[leg['base']]
            amount=number(leg['size'])*(D(10)**m['size_decimals'])
            price=number(leg['price_limit'])*(D(10)**m['price_decimals'])
            if amount!=amount.to_integral_value() or price!=price.to_integral_value(): raise ValueError('SDK integer mismatch')
            _tx,response,error=await asyncio.wait_for(self.trader.client.create_market_order(market_index=m['id'],
                client_order_index=cid,base_amount=int(amount),avg_execution_price=int(price),
                is_ask=leg['side']=='sell',reduce_only=True),timeout=12)
            if response is not None and int(response.code)!=200: return 'rejected'
            if error or response is None: raise ValueError('SDK ack unknown; query only')

    async def confirm(self,leg,cid,before):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            try:
                if leg['venue']=='ondo':
                    raw=await self.rest.request('GET','/v1/perps/orders/client:'+str(cid))
                    result=parse_ondo_close(raw,leg,cid,self.om[leg['base']]['market'])
                    positions=await self.ondo_positions()
                else:
                    data=await trial.get(f'/api/v1/accountOrders?account_index={self.trader.account_index}&client_order_indexes={cid}',self.trader._auth())
                    found=[r for r in data['orders'] if int(r['client_order_index'])==cid]
                    if len(found)!=1: raise ValueError('Lighter order not unique/found')
                    raw=found[0]
                    result=parse_lighter_close(raw,leg,cid,self.lm[leg['base']]['id'],self.trader.account_index)
                    ids={m['id']:b for b,m in self.lm.items()}
                    positions={ids.get(k,'unmanaged:'+str(k)):q for k,q in trial.signed_positions(await self.trader.account()).items()}
                expected={b:number(q) for b,q in before[leg['venue']+'_positions'].items()}
                expected[leg['base']]=number(leg['position_before'])+(number(result['filled']) if leg['side']=='buy' else -number(result['filled']))
                if result['terminal'] and nonzero(positions)==nonzero(expected): return result
            except Exception:
                pass  # Query only. Never sign/retransmit after any uncertainty.
            await asyncio.sleep(1)
        return None


async def run(args):
    from dotenv import dotenv_values
    cfg=load_config(args.config)
    values=dotenv_values(args.env)
    keys={'ondo_api_key_id':'ONDO_API_KEY_ID','ondo_api_secret':'ONDO_API_SECRET',
          'lighter_api_key_private_key':'LIGHTER_API_KEY_PRIVATE_KEY','lighter_account_index':'LIGHTER_ACCOUNT_INDEX','lighter_api_key_index':'LIGHTER_API_KEY_INDEX'}
    secrets=Secrets(**{field:(values.get(name) or '').strip() for field,name in keys.items()})
    if not all(secrets.present().values()): raise ValueError('missing .env-only credential')
    native=Native(cfg,secrets)
    try:
        await native.setup()
        if not args.execute:
            snapshot=await native.snapshot()
            legs=[await native.leg(b,v,q if v=='ondo' else -q) for b,q in SCOPE.items() for v in ('ondo','lighter')]
            return {'snapshot':snapshot,'plans':legs,'worst_cost':str(sum(number(p['worst_cost']) for p in legs)),'orders_sent':0}
        return await execute(native,args.journal)
    finally:
        await native.trader.close()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',default='config.yaml');p.add_argument('--env',default='.env')
    p.add_argument('--journal',required=True);p.add_argument('--execute',action='store_true')
    args=p.parse_args(argv)
    logging.disable(logging.CRITICAL)
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        try: result=asyncio.run(run(args))
        except Exception as exc: result={'phase':'blocked','error_type':type(exc).__name__,'reason':str(exc) if isinstance(exc,ValueError) else 'inspect safely','orders_sent':0}
    print(json.dumps(result,default=str,ensure_ascii=False),flush=True)
    return 0 if result.get('phase') in ('resolved',None) else 2


if __name__=='__main__': raise SystemExit(main())
