"""매시간 공개 확정 펀딩 수집. 키·서명·주문 클라이언트 사용 없음."""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime,timezone
from decimal import Decimal as D, ROUND_DOWN
import gzip
import json
from pathlib import Path
import time
import urllib.parse

from olbot.funding import parse_ondo_history,parse_lighter_history,make_hours,finite,history_window
from olbot.markets import _get_json
from olbot.paper import Book,parse_book,common_step,vwap
from olbot.public_lease import public_rest_lease
from olbot.ratelimit import TokenBucket

ONDO='https://api.ondoperps.xyz'
LIGHTER='https://mainnet.zklighter.elliot.ai'


def cost_snapshot(base,ob,lb,quantity,stamp,source):
    bids,asks=[[(finite(p),finite(q)) for p,q in ob[k]] for k in ('bids','asks')]
    if not bids or not asks or any(p<=0 or q<0 for p,q in bids+asks): raise ValueError('invalid cost levels')
    omid=(bids[0][0]+asks[0][0])/2
    if not 0<bids[0][0]<asks[0][0] or not lb.mid: raise ValueError('invalid funding cost books')
    o_sell,o_buy=vwap(bids,quantity),vwap(asks,quantity)
    l_sell,l_buy=lb.execute(quantity),lb.execute(-quantity)
    out={'base':base,'ts':stamp,'source':source,'quantity':str(quantity),'ondo_mid':str(omid),'lighter_mid':str(lb.mid),
         'ondo_notional_usd':str(quantity*omid),'lighter_notional_usd':str(quantity*lb.mid)}
    for direction in (1,-1):
        # +1 = short Ondo / long Lighter. -1 = long Ondo / short Lighter.
        oe,le,ox,lx=(o_sell,l_buy,o_buy,l_sell) if direction==1 else (o_buy,l_sell,o_sell,l_buy)
        if any(p is None for p in (oe,le,ox,lx)):
            entry=exit_=None
        else:
            entry=D(direction)*quantity*(omid-oe+le-lb.mid)+quantity*oe*D('.00025')
            exit_=D(direction)*quantity*(ox-omid+lb.mid-lx)+quantity*ox*D('.00025')
        out[str(direction)]={'entry_cost':str(entry) if entry is not None else None,'exit_cost':str(exit_) if exit_ is not None else None}
    return out


class FundingWatch:
    bases=('BTC','ETH')
    def __init__(self,comparison,output):
        self.comparison,self.output=Path(comparison),Path(output)
        self.output.mkdir(parents=True,exist_ok=True)
        self.bucket=TokenBucket(1,1)
        self.started=time.time();self.history={b:{'ondo':{},'lighter':{}} for b in self.bases}
        self.meta={};self.seed_costs={};self.calls=0;self.errors=0
        launch=json.loads((self.comparison.parent/'comparison-launch.json').read_text(encoding='utf-8-sig'))
        self.start=datetime.fromisoformat(launch['started_utc']).timestamp()
        self.end=datetime.fromisoformat(launch['deadline_utc']).timestamp()
        saved=self.output/'hourly-latest.json'
        if saved.exists():
            prior=json.loads(saved.read_text(encoding='utf-8'))
            if prior['start_ts']!=self.start or prior['end_ts']!=self.end:
                raise ValueError('funding history window mismatch; preserve prior output')
            self.history={b:{v:{int(h):r for h,r in prior['history'][b][v].items()}
                             for v in ('ondo','lighter')} for b in self.bases}
            self.errors=prior['errors']
            status=self.output/'status.json'
            if status.exists(): self.calls=json.loads(status.read_text(encoding='utf-8'))['requests']

    def save(self,name,data,append=False):
        if append:
            with (self.output/name).open('a',encoding='utf-8') as stream: stream.write(json.dumps(data,default=str,ensure_ascii=False)+'\n')
        else:
            temp=self.output/(name+'.tmp');temp.write_text(json.dumps(data,default=str,ensure_ascii=False),encoding='utf-8');temp.replace(self.output/name)

    async def get(self,url):
        if url.startswith(ONDO): await self.bucket.acquire()
        self.calls+=1
        return await asyncio.to_thread(_get_json,url)

    async def setup(self):
        async with public_rest_lease(self.comparison):
            op=(await self.get(ONDO+'/v1/markets'))['result']['perps']['tradingPairs']
            om={x['pair']['base'].upper():x for x in op if not x.get('disabled')}
            lm=(await self.get(LIGHTER+'/api/v1/orderBooks'))['order_books']
            lm={x['symbol'].upper():x for x in lm if x.get('status')=='active' and x.get('market_type')=='perp'}
            for b in self.bases:
                d=(await self.get(LIGHTER+f"/api/v1/orderBookDetails?market_id={lm[b]['market_id']}"))['order_book_details'][0]
                self.meta[b]={'market':om[b]['market'],'id':int(lm[b]['market_id']),
                              'step':common_step(D(om[b]['baseIncrement']),D(1).scaleb(-int(d['supported_size_decimals'])))}
        seed_path=self.output/'entry-cost-proxies.json'
        if seed_path.exists():
            self.seed_costs=json.loads(seed_path.read_text(encoding='utf-8'))
            if set(self.seed_costs)!=set(self.bases) or any(finite(r['quantity'])<=0 for r in self.seed_costs.values()):
                raise ValueError('invalid original funding cost proxy')
            return  # Preserve original benchmark entry; a restart is not a new entry.
        # Original public books preserve an entry-cost proxy near the original experiment start.
        original={}
        with gzip.open(self.comparison/'reward-inputs.jsonl.gz','rt',encoding='utf-8') as stream:
            for line in stream:
                row=json.loads(line)
                original.setdefault(row['base'],row)
                if len(original)==2: break
        for b in self.bases:
            raw=json.loads((self.comparison/'fixtures'/f"lighter-subscribed-order_book-order_book-{self.meta[b]['id']}.json").read_text(encoding='utf-8'))
            parsed=parse_book(raw);book=Book();book.apply(*parsed[1:],self.start)
            ob=original[b]['book'];omid=(D(ob['bids'][0][0])+D(ob['asks'][0][0]))/2
            step=self.meta[b]['step']
            q=(D(200)/max(omid,book.mid)/step).to_integral_value(rounding=ROUND_DOWN)*step
            seed=cost_snapshot(b,ob,book,q,original[b]['ts'],'original comparison books; venue timestamps may differ')
            self.seed_costs[b]=seed
        self.save('entry-cost-proxies.json',self.seed_costs)

    async def cycle(self):
        now=time.time();end=int(min(now,self.end));start=int(self.start//3600)*3600-3600
        async with public_rest_lease(self.comparison):
            for base in self.bases:
                m=self.meta[base]
                for venue in ('ondo','lighter'):
                    params={'market':m['market'],'startTime':start*1000,'endTime':end*1000,'limit':1000} if venue=='ondo' else {
                        'market_id':m['id'],'resolution':'1h','start_timestamp':start,'end_timestamp':end,'count_back':200}
                    url=(ONDO+'/v1/perps/funding_rate_history?' if venue=='ondo' else LIGHTER+'/api/v1/fundings?')+urllib.parse.urlencode(params)
                    try:
                        raw=await self.get(url)
                        parsed=parse_ondo_history(raw,m['market']) if venue=='ondo' else parse_lighter_history(raw)
                        self.save('raw-history.jsonl',{'fetched_ts':time.time(),'base':base,'venue':venue,'response':raw},True)
                        for h,row in history_window(parsed,start,end).items():
                            prior=self.history[base][venue].get(h)
                            if prior is None or prior['data']!=row:
                                self.history[base][venue][h]={'data':row,'first_seen':prior['first_seen'] if prior else time.time(),'last_seen':time.time()}
                    except Exception as exc:
                        self.errors+=1
                        self.save('errors.jsonl',{'ts':time.time(),'base':base,'venue':venue,'type':type(exc).__name__},True)
                try:
                    ob=(await self.get(ONDO+'/v1/perps/depth?'+urllib.parse.urlencode({'market':m['market'],'depth':100})))['result']
                    raw=await self.get(LIGHTER+f"/api/v1/orderBookOrders?market_id={m['id']}&limit=100")
                    sides=[[(finite(r['price']),finite(r['remaining_base_amount'])) for r in raw[k]] for k in ('bids','asks')]
                    book=Book();book.apply(True,0,None,sides,time.time())
                    cost=cost_snapshot(base,ob,book,D(self.seed_costs[base]['quantity']),time.time(),'hourly public books, fixed initial quantity')
                    self.save('exit-cost-proxies.jsonl',cost,True)
                except Exception as exc:
                    self.errors+=1;self.save('errors.jsonl',{'ts':time.time(),'base':base,'type':type(exc).__name__,'kind':'cost'},True)
        rows=[];seeds={}
        for base in self.bases:
            o={h:r['data'] for h,r in self.history[base]['ondo'].items()}
            l={h:r['data'] for h,r in self.history[base]['lighter'].items()}
            rows+=make_hours(base,o,l,self.start,self.end,now)
            candidates=sorted(set(o)&set(l))
            candidates=[h for h in candidates if h<=self.start]
            if candidates:
                h=candidates[-1];seeds[base]={'hour':h,'difference':str(D(o[h]['rate'])-D(l[h]['rate'])),
                    'note':'preceding settled timestamp; original publication delay not retrospectively proven'}
            else: seeds[base]=None
        data={'start_ts':self.start,'end_ts':self.end,'updated_ts':now,'rows':rows,'seeds':seeds,
              'history':self.history,'orders_sent':0,'credentials_read':False,'errors':self.errors}
        self.save('hourly-latest.json',data)
        self.save('hourly-log.jsonl',{'fetched_ts':now,'rows':rows},True)
        self.save('status.json',{'updated_ts':time.time(),'state':'observing','matched_hours':sum(not r['missing'] for r in rows),
                               'missing_rows':sum(bool(r['missing']) for r in rows),'requests':self.calls,'errors':self.errors,
                               'orders_sent':0,'credentials_read':False})
        from olbot.paper_watch import write_report
        for attempt in range(3):
            try:
                write_report(self.comparison,funding_folder=self.output)
                break
            except (EOFError, json.JSONDecodeError):
                if attempt==2: raise
                await asyncio.sleep(1)

    async def run(self):
        try:
            await self.setup()
            while time.time()<self.end and not (self.output/'STOP').exists():
                try:
                    await self.cycle()
                except Exception as exc:
                    self.errors+=1;self.save('errors.jsonl',{'ts':time.time(),'kind':'cycle','type':type(exc).__name__},True)
                now=time.time();next_hour=int(now//3600)*3600+300
                if next_hour<=now: next_hour+=3600
                await asyncio.sleep(max(0,min(next_hour,self.end)-now))
            await self.cycle()  # Final fetch includes the last settled hour before the deadline.
        finally:
            state='finished' if time.time()>=self.end else 'stopped_or_failed'
            self.save('finished.json',{'ts':time.time(),'state':state,'orders_sent':0,'credentials_read':False})
            if (self.output/'status.json').exists():
                status=json.loads((self.output/'status.json').read_text(encoding='utf-8'))
                status.update(state=state,updated_ts=time.time())
                self.save('status.json',status)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--comparison',required=True);p.add_argument('--output',required=True)
    p.add_argument('--once',action='store_true',help='공개 수집/보고서 경로 1회 확인, 반복 없음')
    a=p.parse_args(argv)
    async def run():
        watcher=FundingWatch(a.comparison,a.output)
        if a.once:
            await watcher.setup();await watcher.cycle()
            status=json.loads((watcher.output/'status.json').read_text(encoding='utf-8'))
            status['state']='sample_complete';watcher.save('status.json',status)
        else:
            await watcher.run()
    asyncio.run(run())
    return 0


if __name__=='__main__': raise SystemExit(main())
