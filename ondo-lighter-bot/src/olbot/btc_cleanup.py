"""Bounded BTC trial cleanup only. Never reuse the exhausted LIT/ENA approval."""
from decimal import Decimal as D,ROUND_CEILING,ROUND_FLOOR
from pathlib import Path
import asyncio,time

from olbot.close_positions import Native,number,nonzero,cost_at_reference
from olbot.paper import vwap
from olbot import trial,ondo_trial,paired_trial

MAX_NOTIONAL=D(100)
MAX_COST=D(2)


def make_btc_leg(venue,position,bids,asks,tick,step,min_base,min_quote):
    if venue not in ('ondo','lighter'):raise ValueError('BTC venue outside scope')
    position,tick,step=map(number,(position,tick,step));size=abs(position)
    if not size or tick<=0 or step<=0 or size%step or size<number(min_base):raise ValueError('BTC close size/minimum invalid')
    bids=sorted([(number(p),number(q)) for p,q in bids if number(q)>0],reverse=True)
    asks=sorted([(number(p),number(q)) for p,q in asks if number(q)>0])
    if not bids or not asks or not 0<bids[0][0]<asks[0][0]:raise ValueError('BTC depth invalid')
    side='sell' if position>0 else 'buy';best=(bids if position>0 else asks)[0][0]
    raw=best*(D('.998') if position>0 else D('1.002'))
    limit=(raw/tick).to_integral_value(rounding=ROUND_CEILING if position>0 else ROUND_FLOOR)*tick
    levels=[(p,q) for p,q in (bids if position>0 else asks) if (p>=limit if position>0 else p<=limit)]
    execution=vwap(levels,size)
    if execution is None:raise ValueError('BTC protected depth insufficient')
    if max(size*best,size*execution,size*limit)>MAX_NOTIONAL or size*execution<number(min_quote):raise ValueError('BTC close notional outside scope')
    mid=(bids[0][0]+asks[0][0])/2;sign=D(1) if position>0 else D(-1);fee=D('.00025') if venue=='ondo' else D(0)
    return {'base':'BTC','venue':venue,'side':side,'size':str(size),'position_before':str(position),'price_limit':str(limit),
            'mid':str(mid),'expected_vwap':str(execution),'expected_cost':str(sign*size*(mid-execution)+size*execution*fee),
            'worst_cost':str(sign*size*(mid-limit)+size*limit*fee),'tick':str(tick)}


class BtcNative(Native):
    async def setup(self):
        trial.require_resolved_trial(self.cfg);ondo_trial.require_resolved_trial(self.cfg);paired_trial.require_resolved_trial(self.cfg)
        await self.trader.connect()
        self.om={r['pair']['base']:r for r in (await self.rest.request('GET','/v1/markets'))['perps']['tradingPairs'] if not r.get('disabled')}
        books=(await trial.get('/api/v1/orderBooks'))['order_books']
        matches=[r for r in books if r['symbol']=='BTC' and r['market_type']=='perp' and r['status']=='active']
        if len(matches)!=1:raise ValueError('BTC metadata not unique')
        mid=int(matches[0]['market_id']);d=(await trial.get(f'/api/v1/orderBookDetails?market_id={mid}'))['order_book_details'][0]
        self.lm={'BTC':{'id':mid,'size_decimals':int(d['supported_size_decimals']),'price_decimals':int(d['supported_price_decimals']),
                        'min_base':number(d['min_base_amount']),'min_quote':number(d['min_quote_amount'])}}

    async def leg(self,base,venue,position):
        if base!='BTC':raise ValueError('cleanup is BTC only')
        if venue=='ondo':
            raw=await self.rest.request('GET','/v1/perps/depth',params={'market':self.om[base]['market'],'depth':100})
            bids,asks=raw['bids'],raw['asks'];tick=number(self.om[base]['quoteIncrement']);step=number(self.om[base]['baseIncrement']);minimum=step;quote=D(0)
        elif venue=='lighter':
            m=self.lm[base];raw=await trial.get(f"/api/v1/orderBookOrders?market_id={m['id']}&limit=100")
            bids,asks=[[(r['price'],r['remaining_base_amount']) for r in raw[k]] for k in ('bids','asks')]
            tick=D(1).scaleb(-m['price_decimals']);step=D(1).scaleb(-m['size_decimals']);minimum=m['min_base'];quote=m['min_quote']
        else:raise ValueError('venue outside scope')
        return make_btc_leg(venue,position,bids,asks,tick,step,minimum,quote)


def check_snapshot(snapshot):
    if snapshot['ondo_open'] or snapshot['lighter_open'] or snapshot['pending']:raise ValueError('active/pending orders; no cleanup send')
    for venue in ('ondo','lighter'):
        if any(b!='BTC' and number(q) for b,q in snapshot[venue+'_positions'].items()):raise ValueError('non-BTC position; no cleanup send')


async def execute(adapter,path,*,approved=False):
    """Caller must have explicit approval for BTC<=100USD/leg,20bps,2USD,<=2reduce-onlyIOC."""
    if not approved:raise ValueError('specific BTC cleanup approval required')
    path=Path(path)
    if path.exists():raise ValueError('cleanup journal exists; query only, never execute again')
    state={'phase':'preflight','orders_sent':0,'attempts':[],'started_ts':time.time(),'scope':'BTC only,100USD/leg,20bps,2USD,2orders,no retry'}
    trial.save(path,state)
    try:
        before=await adapter.snapshot();check_snapshot(before);state['before']=before
        positions={v:number(before[v+'_positions'].get('BTC',0)) for v in ('ondo','lighter')}
        plans={v:await adapter.leg('BTC',v,q) for v,q in positions.items() if q}
        if sum(number(p['worst_cost']) for p in plans.values())>MAX_COST:raise ValueError('BTC cleanup cost budget exceeded')
        references={v:number(p['mid']) for v,p in plans.items()};used=D(0)
        remaining=dict(plans)
        for venue in ('ondo','lighter'):
            if not positions[venue]:continue
            current=await adapter.snapshot();check_snapshot(current)
            expected={v:positions[v] if v in remaining else D(0) for v in ('ondo','lighter')}
            if any(number(current[v+'_positions'].get('BTC',0))!=q for v,q in expected.items()):raise ValueError('BTC positions changed before cleanup send')
            leg=await adapter.leg('BTC',venue,positions[venue]);sign=D(1) if leg['side']=='sell' else D(-1)
            cap=sign*number(leg['size'])*(references[venue]-number(leg['price_limit']))
            cap+=number(leg['size'])*number(leg['price_limit'])*(D('.00025') if venue=='ondo' else D(0))
            if used+cap+sum(number(p['worst_cost']) for v,p in remaining.items() if v!=venue)>MAX_COST:raise ValueError('refreshed BTC cost exceeds budget')
            cid=adapter.order_id(venue);attempt={'leg':leg,'client_id':cid,'phase':'send_unknown','sent_ts':time.time()}
            state['attempts'].append(attempt);state['orders_sent']+=1
            if state['orders_sent']>2:raise ValueError('BTC cleanup order budget exhausted')
            trial.save(path,state)  # fsync before the one permitted send of this leg.
            try:attempt['ack']=await adapter.send(leg,cid)
            except Exception as exc:attempt['ack']='unknown:'+type(exc).__name__
            trial.save(path,state)
            if attempt['ack']=='rejected':raise ValueError('BTC cleanup rejected; no retry')
            result=await adapter.confirm(leg,cid,current)
            if result is None or not result['terminal'] or number(result['filled'])!=abs(positions[venue]):raise ValueError('BTC cleanup unknown/partial; no retry or next send')
            attempt['result']=result;attempt['phase']='final_position_confirmed';used+=cost_at_reference(leg,result,references[venue]);remaining.pop(venue)
            trial.save(path,state)
        final=await adapter.snapshot();check_snapshot(final)
        if nonzero(final['ondo_positions']) or nonzero(final['lighter_positions']):raise ValueError('BTC residual after cleanup')
        await asyncio.sleep(1.1);second=await adapter.snapshot();check_snapshot(second)
        if nonzero(second['ondo_positions']) or nonzero(second['lighter_positions']):raise ValueError('BTC final positions changed')
        state.update(phase='resolved',after=final,after_second=second,execution_cost_vs_reference=str(used))
    except Exception as exc:
        state.update(phase='halted',error_type=type(exc).__name__,reason=str(exc) if isinstance(exc,ValueError) else 'cleanup error; inspect without resending',urgent=bool(state['attempts']))
        try:
            state['after']=await adapter.snapshot()
            after=state['after']
            state['urgent']=state['urgent'] or bool(after['ondo_open'] or after['lighter_open'] or after['pending']) or (
                number(after['ondo_positions'].get('BTC',0))+number(after['lighter_positions'].get('BTC',0))!=0)
        except Exception:state['after_snapshot_failed']=True
    state['ended_ts']=time.time();trial.save(path,state);return state
