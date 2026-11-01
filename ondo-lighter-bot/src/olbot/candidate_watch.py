"""Keyless TradFi paper candidates, isolated from the original BTC/ETH cohort."""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime,timezone
from decimal import Decimal as D
import gzip
import json
from pathlib import Path
import random
import time
from zoneinfo import ZoneInfo

from olbot.paper_watch import Watch,OndoPaperWS
from olbot.paper import reward_scenario
from olbot.rewards import load_policy

BASES=('META','TSLA','NVDA','XAG','XAU','WTI')
STOCKS={'META','TSLA','NVDA'}


def parse_depth(msg):
    if msg.get('type')!='update' or msg.get('channel')!='depthBooksPerps':return []
    out=[]
    for row in msg['data']:
        stamp=datetime.fromisoformat(row['time'].replace('Z','+00:00'))
        if stamp.tzinfo is None:raise ValueError('depth timezone missing')
        book={}
        for side in ('bids','asks'):
            values=[(D(p),D(q)) for p,q in row[side]]
            if any(not p.is_finite() or not q.is_finite() or p<=0 or q<0 for p,q in values):raise ValueError('invalid depth')
            values=sorted(((p,q) for p,q in values if q),reverse=side=='bids')
            book[side]=[[str(p),str(q)] for p,q in values]
        if not book['bids'] or not book['asks'] or D(book['bids'][0][0])>=D(book['asks'][0][0]):raise ValueError('empty/crossed depth')
        out.append((row['market'].split('-')[0],book,stamp.timestamp()))
    return out


def session_label(base,stamp):
    if base not in STOCKS:return 'commodity_24h_session_unclassified'
    local=datetime.fromtimestamp(stamp,ZoneInfo('America/New_York'))
    if local.year!=2026:return 'calendar_unverified'
    holidays={'2026-01-01','2026-01-19','2026-02-16','2026-04-03','2026-05-25','2026-06-19','2026-07-03','2026-09-07','2026-11-26','2026-12-25'}
    date=local.date().isoformat()
    if local.weekday()>=5:return 'weekend'
    if date in holidays:return 'holiday'
    close=13*60 if date in {'2026-11-27','2026-12-24'} else 16*60
    return 'regular' if 9*60+30<=local.hour*60+local.minute<close else 'outside_regular'


class CandidateOndoWS(OndoPaperWS):
    def subscriptions(self):
        return super().subscriptions()+[{'op':'subscribe','channel':'depthBooksPerps',
            'markets':[self.owner.meta[b]['market'] for b in self.owner.bases],'limit':100}]

    def on_message(self,msg,recv_ts):
        super().on_message(msg,recv_ts)
        for base,book,source_ts in parse_depth(msg):
            if base in self.owner.bases:self.owner.depths[base]=(book,recv_ts)


class CandidateWatch(Watch):
    bases=BASES

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.ondo_ws=CandidateOndoWS(self)

    def group(self,scenario):
        return [x for x in self.scenarios if (x.base,x.distance,x.model)==(scenario.base,scenario.distance,scenario.model)]

    def event(self,venue,kind,detail=None):
        if venue=='paper_ondo_ws' and kind in {'error','disconnected','closed'}:self.depths.clear()
        super().event(venue,kind,detail)

    def write(self,file,value,append=False):
        if isinstance(value,dict) and value.get('base') in self.bases and file in {'fills.jsonl','hedges.jsonl'}:
            value={**value,'session':session_label(value['base'],value.get('recv_ts',value.get('ts',time.time())))}
        super().write(file,value,append)

    def summary(self,now,final=False):
        data=super().summary(now,final)
        data.update(bases=list(self.bases),independent_candidates=True,
                    capital_per_alternative_usd='500',cohort='tradfi_started_20261101',
                    observation_window_seconds=now-self.started,
                    note='Independent alternatives, no sum across assets; starts later than original BTC/ETH cohort.')
        for row in data['rows']:row['session_now']=session_label(row['base'],now)
        return data

    async def rewards(self):
        await asyncio.sleep(random.uniform(5,15))
        while time.time()<self.deadline and not self.stop.is_set():
            try:
                policy=load_policy(self.policy_path);now=time.time()
                policies=self.output/'policies';policies.mkdir(exist_ok=True)
                (policies/(str(int(policy.starts))+'.json')).write_text(Path(self.policy_path).read_text(encoding='utf-8'),encoding='utf-8')
                span=min(90,now-self.reward_ts) if self.reward_ts is not None else 0
                for base in self.bases:
                    depth=self.depths.get(base)
                    valid=depth is not None and now-depth[1]<=65 and self.healthy(base,now)
                    book=depth[0] if valid else None
                    with gzip.open(self.output/'reward-inputs.jsonl.gz','at',encoding='utf-8') as stream:
                        stream.write(json.dumps({'ts':now,'base':base,'book':book,'session':session_label(base,now),
                            'coverage':'observed' if valid else 'missing_or_stale',
                            'orders':[{'distance':s.distance,'model':s.model,'levels':[[str(o.price),str(o.remaining)] for o in s.orders.values() if o.active_at<=now]}
                                      for s in self.scenarios if s.base==base]},ensure_ascii=False)+'\n')
                for s in self.scenarios:
                    depth=self.depths.get(s.base)
                    valid=depth is not None and now-depth[1]<=65 and self.healthy(s.base,now) and policy.starts<=now<policy.ends
                    own=[(o.price,o.remaining) for o in s.orders.values() if o.active_at<=now]
                    estimate=(reward_scenario(depth[0],own,policy.per_market_usdc) if s.base in policy.allowed_bases else (D(0),D(0),False)) if valid else None
                    if estimate is None:s.reward_unknown+=1;continue
                    low,high,ambiguous=estimate
                    s.reward_values.append(low);s.reward_high_values.append(high)
                    s.reward_accrued_low+=low*D(str(span))/604800;s.reward_accrued_high+=high*D(str(span))/604800
                    self.write('rewards.jsonl',{'ts':now,'base':s.base,'distance':s.distance,'model':s.model,
                        'low_week_usdc':str(low),'high_week_usdc':str(high),'interpolation_sec':span,
                        'ambiguous_boundary':ambiguous,'session':session_label(s.base,now),'policy_source':policy.source},True)
                self.reward_ts=now
            except Exception as exc:self.event('candidate_sampling','error',type(exc).__name__)
            try:await asyncio.wait_for(self.stop.wait(),timeout=random.uniform(45,75))
            except asyncio.TimeoutError:pass


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True);p.add_argument('--policy',required=True)
    p.add_argument('--deadline',required=True);p.add_argument('--rest-coordination',required=True)
    a=p.parse_args(argv);deadline=datetime.fromisoformat(a.deadline).timestamp()
    if not time.time()<deadline<=time.time()+604800:p.error('invalid fixed research deadline')
    if (Path(a.output)/'summary.json').exists():p.error('preserve existing candidate cohort; no implicit restart')
    asyncio.run(CandidateWatch(a.output,a.policy,deadline,rest_coordination=a.rest_coordination).run())
    return 0


if __name__=='__main__':raise SystemExit(main())
