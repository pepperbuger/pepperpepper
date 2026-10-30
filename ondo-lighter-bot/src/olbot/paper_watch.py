"""BTC/ETH 세 거리 공개 관측. 이 모듈은 키/주문 클라이언트를 import하지 않는다."""
from __future__ import annotations

import argparse
import asyncio
import gzip
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal as D
import json
import random
from pathlib import Path
import time

from olbot.market import MarketBoard, parse_ondo_top_of_book
from olbot.markets import _get_json
from olbot.ondo.transport import ws_connect_options
from olbot.paper import (Book, Scenario, common_step, parse_book, parse_trades,
                        parse_ondo_funding, parse_lighter_funding, reward_scenario, vwap)
from olbot.ratelimit import TokenBucket
from olbot.rewards import load_policy
from olbot.wsbase import ReconnectingWS

ONDO = 'https://api.ondoperps.xyz'
LIGHTER = 'https://mainnet.zklighter.elliot.ai'


class OndoPaperWS(ReconnectingWS):
    name = 'paper_ondo_ws'

    def __init__(self, owner):
        super().__init__('wss://api.ondoperps.xyz/ws', 30, {'op': 'ping'}, owner.event)
        self.owner = owner

    def connect_options(self): return ws_connect_options()

    def subscriptions(self):
        markets = [self.owner.meta[b]['market'] for b in self.owner.bases]
        return [{'op': 'subscribe', 'channel': channel, 'markets': markets,
                 **({'numPastTrades': 0} if channel == 'tradesPerps' else {})}
                for channel in ('topOfBooksPerps', 'tradesPerps', 'fundingRatesPerps')]

    def on_message(self, msg, recv_ts):
        self.owner.fixture('ondo', msg)
        self.owner.message_counts['ondo'] += 1
        if msg.get('type') == 'error': raise ValueError('Ondo public subscription error')
        for base, bid, ask in parse_ondo_top_of_book(msg):
            self.owner.board.update_ondo(base, bid, ask, recv_ts)
        for base, rate in parse_ondo_funding(msg):
            self.owner.orates[base] = (rate, recv_ts)
        for trade in parse_trades(msg):
            self.owner.on_trade(trade, recv_ts)


class LighterPaperWS(ReconnectingWS):
    name = 'paper_lighter_ws'

    def __init__(self, owner):
        super().__init__('wss://mainnet.zklighter.elliot.ai/stream?readonly=true', 60, {'type': 'ping'}, owner.event)
        self.owner = owner

    def subscriptions(self):
        return [{'type': 'subscribe', 'channel': f'{channel}/{mid}'}
                for mid in self.owner.base_by_id for channel in ('order_book', 'market_stats')]

    def on_message(self, msg, recv_ts):
        self.owner.fixture('lighter', msg)
        self.owner.message_counts['lighter'] += 1
        if 'error' in msg: raise ValueError('Lighter public subscription error')
        parsed = parse_book(msg)
        if parsed:
            mid, snapshot, nonce, begin, sides = parsed
            self.owner.books[self.owner.base_by_id[mid]].apply(snapshot, nonce, begin, sides, recv_ts)
        funding = parse_lighter_funding(msg)
        if funding:
            mid, rate = funding
            base = self.owner.base_by_id[mid]
            self.owner.lrates[base] = (rate, recv_ts)


class Watch:
    bases = ('BTC', 'ETH')

    def __init__(self, output, policy, deadline, no_rest=False):
        self.output, self.policy_path, self.deadline = Path(output), policy, deadline
        self.output.mkdir(parents=True, exist_ok=True)
        self.board = MarketBoard(list(self.bases))
        self.books = {b: Book() for b in self.bases}
        self.bucket = TokenBucket(1, 1)
        self.meta, self.lmeta, self.base_by_id = {}, {}, {}
        self.scenarios = []
        self.orates, self.lrates = {}, {}
        self.depths = {}
        self.seen, self.seen_queue = set(), deque()
        self.message_counts = {'ondo': 0, 'lighter': 0}
        self.event_counts = {}
        self.fixture_keys = set()
        self.reward_ts = None
        self.started = time.time()
        self.stop = asyncio.Event()
        self.no_rest = no_rest
        self.ondo_ws, self.lighter_ws = OndoPaperWS(self), LighterPaperWS(self)

    async def get(self, url):
        if not url.startswith(ONDO):
            return await asyncio.to_thread(_get_json, url)
        while True:
            while (self.output/'PAUSE_REST').exists(): await asyncio.sleep(.1)
            await self.bucket.acquire()
            if not (self.output/'PAUSE_REST').exists(): break
        active = self.output/'REST_ACTIVE'
        active.write_text(str(time.time()),encoding='utf-8')
        try:
            return await asyncio.to_thread(_get_json, url)
        finally:
            active.unlink(missing_ok=True)

    def write(self, file, value, append=False):
        text = json.dumps(value, default=str, ensure_ascii=False)
        with (self.output / file).open('a' if append else 'w', encoding='utf-8') as stream:
            stream.write(text + '\n')

    def event(self, source, kind, detail=''):
        self.event_counts[source + ':' + kind] = self.event_counts.get(source + ':' + kind, 0) + 1
        self.write('events.jsonl', {'ts': time.time(), 'source': source, 'kind': kind, 'detail': detail}, True)
        if kind in ('disconnected', 'closed', 'error'):
            for s in self.scenarios: s.orders.clear()
            if source == 'paper_lighter_ws':
                for book in self.books.values(): book.clear()
            if source == 'paper_ondo_ws':
                for state in self.board.states.values(): state.ondo = None

    def fixture(self, source, msg):
        if msg.get('type') not in ('update', 'subscribed/order_book', 'update/order_book',
                                   'subscribed/market_stats', 'update/market_stats'):
            return
        key = source + '-' + msg['type'].replace('/', '-') + '-' + str(msg.get('channel', '')).replace(':', '-')
        if key not in self.fixture_keys:
            self.fixture_keys.add(key)
            folder = self.output / 'fixtures'; folder.mkdir(exist_ok=True)
            (folder / (key+'.json')).write_text(json.dumps(msg, ensure_ascii=False), encoding='utf-8')

    def healthy(self, base, now):
        oq, book = self.board.states[base].ondo, self.books[base]
        return bool(self.ondo_ws.connected and self.lighter_ws.connected and oq and book.mid
                    and self.ondo_ws.stale_for(now) <= 65 and self.lighter_ws.stale_for(now) <= 65
                    and now - oq.recv_ts <= 65 and now - book.ts <= 65)

    def on_trade(self, trade, recv_ts):
        base, tid, aggressor, price, size, ts = trade
        if base not in self.bases or tid in self.seen: return
        self.seen.add(tid); self.seen_queue.append(tid)
        if len(self.seen_queue) > 20000: self.seen.discard(self.seen_queue.popleft())
        oq = self.board.states[base].ondo
        for s in self.scenarios:
            if s.base != base: continue
            fill = s.trade(aggressor, price, size, ts, recv_ts, oq.mid if oq else None, self.healthy(base, recv_ts))
            if fill:
                self.write('fills.jsonl', {'recv_ts': recv_ts, 'trade_ts': ts, 'source_trade_id': tid,
                                         'base': base, 'distance': s.distance, 'model': s.model, **fill}, True)

    async def setup(self):
        om = (await self.get(ONDO+'/v1/markets'))['result']['perps']['tradingPairs']
        self.meta = {r['pair']['base'].upper(): r for r in om if not r.get('disabled')}
        lm = (await self.get(LIGHTER+'/api/v1/orderBooks'))['order_books']
        active = {r['symbol']: r for r in lm if r.get('market_type') == 'perp' and r.get('status') == 'active'}
        for b in self.bases:
            if b not in self.meta or b not in active: raise ValueError('공개 관측 마켓 비활성')
            mid = int(active[b]['market_id']); self.base_by_id[mid] = b
            detail = (await self.get(LIGHTER+f'/api/v1/orderBookDetails?market_id={mid}'))['order_book_details'][0]
            self.lmeta[b] = detail
            step = common_step(D(self.meta[b]['baseIncrement']), D(1).scaleb(-int(detail['supported_size_decimals'])))
            for distance in (5, 10, 18):
                for model in ('touch', 'through'):
                    scenario = Scenario(b, distance, model, D(self.meta[b]['quoteIncrement']), step,
                                        D(detail['min_quote_amount']), D(detail['min_base_amount']))
                    scenario.last_quote = self.started - (10 if b == 'BTC' else 5)
                    self.scenarios.append(scenario)
        self.write('method.json', {'version': 1, 'started_ts': self.started, 'deadline_ts': self.deadline,
                                  'size_per_side_usd': '60', 'offsets_bps': [5,10,18], 'refresh_sec': 10,
                                  'replace_gap_sec': 2, 'hedge_delay_sec': '.300', 'maker_fee': '.0001',
                                  'lighter_taker_fee': '0', 'orders_sent': 0,
                                  'note': 'touch/through 시나리오이며 실제 체결 확률의 통계적 신뢰구간이 아니다.'})

    async def rewards(self):
        await asyncio.sleep(random.uniform(5, 15))
        while time.time() < self.deadline and not self.stop.is_set():
            try:
                policy = load_policy(self.policy_path)
                for base in self.bases:
                    raw = await self.get(ONDO+'/v1/perps/depth?market='+self.meta[base]['market']+'&depth=100')
                    if raw.get('success') is not True: raise ValueError('Ondo depth failed')
                    self.depths[base] = (raw['result'], time.time())
                now = time.time()
                policies = self.output/'policies'; policies.mkdir(exist_ok=True)
                archive = policies / (str(int(policy.starts))+'.json')
                archive.write_text(Path(self.policy_path).read_text(encoding='utf-8'), encoding='utf-8')
                span = min(90, now-self.reward_ts) if self.reward_ts is not None else 0
                valid_time = policy.starts <= now < policy.ends
                for base in self.bases:
                    with gzip.open(self.output/'reward-inputs.jsonl.gz', 'at', encoding='utf-8') as stream:
                        stream.write(json.dumps({'ts': now, 'base': base, 'book': self.depths[base][0],
                            'orders': [{'distance': s.distance, 'model': s.model,
                                        'levels': [[str(o.price),str(o.remaining)] for o in s.orders.values() if o.active_at <= now]}
                                       for s in self.scenarios if s.base == base]}, ensure_ascii=False)+'\n')
                for s in self.scenarios:
                    raw, stamp = self.depths[s.base]
                    orders = [(o.price, o.remaining) for o in s.orders.values() if o.active_at <= now]
                    if not valid_time:
                        s.reward_unknown += 1
                        continue
                    estimate = (D(0), D(0), False) if s.base not in policy.allowed_bases else reward_scenario(raw, orders, policy.per_market_usdc)
                    if estimate is None:
                        s.reward_unknown += 1
                        continue
                    low, high, ambiguous = estimate
                    s.reward_values.append(low); s.reward_high_values.append(high)
                    s.reward_accrued_low += low * D(str(span)) / 604800
                    s.reward_accrued_high += high * D(str(span)) / 604800
                    self.write('rewards.jsonl', {'ts': now, 'base': s.base, 'distance': s.distance, 'model': s.model,
                                              'low_week_usdc': str(low), 'high_week_usdc': str(high),
                                              'ambiguous_boundary': ambiguous, 'policy_source': policy.source,
                                              'interpolation_sec': span}, True)
                self.reward_ts = now
            except Exception as exc:
                self.event('paper_rest', 'error', type(exc).__name__)
            try: await asyncio.wait_for(self.stop.wait(), timeout=random.uniform(45,75))
            except asyncio.TimeoutError: pass

    def summary(self, now, final=False):
        rows = []
        elapsed = max(now - self.started, .001)
        for s in self.scenarios:
            oq, book = self.board.states[s.base].ondo, self.books[s.base]
            row = s.result(oq.mid if oq else None, book, now)
            exit_cost = None
            if oq and book.mid and s.base in self.depths:
                raw, stamp = self.depths[s.base]
                levels = [(D(p), D(q)) for p,q in raw['bids' if s.pos > 0 else 'asks']]
                op = vwap(levels, abs(s.pos)) if s.pos else oq.mid
                lp = book.execute(s.lpos) if s.lpos else book.mid
                if op is not None and lp is not None and now-stamp < 330:
                    exit_cost = s.pos*(oq.mid-op)+s.lpos*(book.mid-lp)+abs(s.pos*op)*D('.00025')
            mark = row['mark_pnl_ex_reward_funding_usd']
            known = mark is not None and exit_cost is not None and s.funding_unknown_seconds < 1 and not s.pending
            row['exit_cost_estimate_usd'] = str(exit_cost) if exit_cost is not None else None
            row['fills_week_extrapolated'] = str(D(s.fills) * 604800 / D(str(elapsed)))
            row['net_observed_low_usdc'] = row['net_observed_high_usdc'] = None
            row['net_week_low_usdc'] = row['net_week_high_usdc'] = None
            if known and s.reward_values and s.reward_unknown == 0:
                pnl = D(mark) + s.funding_estimate
                row['net_observed_low_usdc'] = str(pnl+s.reward_accrued_low-exit_cost)
                row['net_observed_high_usdc'] = str(pnl+s.reward_accrued_high-exit_cost)
                scale = D(604800)/D(str(elapsed))
                if not s.killed:
                    row['net_week_low_usdc'] = str(pnl*scale + D(row['reward_scenario_week_usdc_mean']) - exit_cost)
                    row['net_week_high_usdc'] = str(pnl*scale + D(row['reward_scenario_week_usdc_high_mean']) - exit_cost)
            rows.append(row)
        return {'version':1, 'updated_ts': now, 'started_ts': self.started, 'deadline_ts': self.deadline,
                'state': 'finished' if final else 'observing', 'orders_sent': 0,
                'connected': {'ondo': self.ondo_ws.connected, 'lighter': self.lighter_ws.connected},
                'messages': self.message_counts, 'events': self.event_counts, 'rows': rows,
                'warning': '가상 체결/지연 시나리오. 실제 수익 아님. 순이익 null은 불확실·자료 부족이며 0이 아님.'}

    async def run(self):
        await self.setup()
        tasks = [asyncio.create_task(self.ondo_ws.run()), asyncio.create_task(self.lighter_ws.run()),
                 *([] if self.no_rest else [asyncio.create_task(self.rewards())])]
        recorded = 0
        try:
            while time.time() < self.deadline and not (self.output/'STOP').exists():
                now = time.time()
                for s in self.scenarios:
                    oq, book = self.board.states[s.base].ondo, self.books[s.base]
                    healthy = self.healthy(s.base, now)
                    group = [x for x in self.scenarios if x.distance == s.distance and x.model == s.model]
                    inventory = sum((abs(x.pos*(self.board.states[x.base].ondo.mid if self.board.states[x.base].ondo else 0)) for x in group), D(0))
                    if oq:
                        s.quote(oq.mid, oq.bid, oq.ask, now, healthy, inventory+D(60) <= 350)
                    orate = self.orates.get(s.base)
                    lrate = self.lrates.get(s.base)
                    result = s.advance(now, oq.mid if oq else None, book, healthy,
                                       orate[0] if orate and now-orate[1]<3600 else None,
                                       lrate[0] if lrate and now-lrate[1]<3600 else None)
                    if result: self.write('hedges.jsonl', {'ts': now, 'base': s.base, 'distance': s.distance, 'model': s.model, **result}, True)
                    group_pnl = sum((x.cash+x.pos*self.board.states[x.base].ondo.mid+x.lpos*self.books[x.base].mid
                                     for x in group if self.board.states[x.base].ondo and self.books[x.base].mid), D(0))
                    if group_pnl < -25:
                        for x in group: x.killed = x.killed or 'group_loss_25_usd'
                if now-recorded >= 30:
                    summary = self.summary(now)
                    self.write('summary.json', summary)
                    self.write('summary-history.jsonl', summary, True)
                    recorded = now
                await asyncio.sleep(.025)
        finally:
            self.stop.set()
            self.ondo_ws.stop(); self.lighter_ws.stop()
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.write('summary.json', self.summary(time.time(), True))
            write_report(self.output)


def write_report(folder):
    folder = Path(folder)
    data = json.loads((folder/'summary.json').read_text(encoding='utf-8'))
    # 유효 공지를 뒤늦게 확인한 주간은 보존한 원본 깊이와 당시 가상 호가로 재계산한다.
    policies = [load_policy(path) for path in (folder/'policies').glob('*.json')] if (folder/'policies').exists() else []
    rewards = {}
    if (folder/'reward-inputs.jsonl.gz').exists():
        with gzip.open(folder/'reward-inputs.jsonl.gz', 'rt', encoding='utf-8') as stream:
            for line in stream:
                sample = json.loads(line)
                policy = next((p for p in policies if p.starts <= sample['ts'] < p.ends), None)
                for order in sample['orders']:
                    key = (sample['base'],order['distance'],order['model'])
                    agg = rewards.setdefault(key, {'low':D(0),'high':D(0),'accrued_low':D(0),'accrued_high':D(0),'n':0,'unknown':0,'ts':sample['ts']})
                    dt = sample['ts']-agg['ts']; agg['ts'] = sample['ts']
                    estimate = None if policy is None else ((D(0),D(0),False) if sample['base'] not in policy.allowed_bases else
                        reward_scenario(sample['book'],[(D(p),D(q)) for p,q in order['levels']],policy.per_market_usdc))
                    if estimate is None or dt > 90:
                        agg['unknown'] += 1
                        continue
                    low, high, _ = estimate
                    agg['low'] += low; agg['high'] += high; agg['n'] += 1
                    agg['accrued_low'] += low*D(str(dt))/604800
                    agg['accrued_high'] += high*D(str(dt))/604800
        elapsed = D(str(data['updated_ts']-data['started_ts']))
        for row in data['rows']:
            a = rewards.get((row['base'],row['distance_bps'],row['model']))
            if not a or not a['n']: continue
            row['reward_scenario_week_usdc_mean'] = str(a['low']/a['n'])
            row['reward_scenario_week_usdc_high_mean'] = str(a['high']/a['n'])
            row['reward_unknown_samples'] = a['unknown']
            if not a['unknown'] and row['exit_cost_estimate_usd'] is not None and row['mark_pnl_ex_reward_funding_usd'] is not None and row['funding_unknown_seconds'] < 1 and not row['pending_hedge']:
                pnl = D(row['mark_pnl_ex_reward_funding_usd'])+D(row['funding_estimate_usd'])
                exit_cost = D(row['exit_cost_estimate_usd'])
                row['net_observed_low_usdc'] = str(pnl+a['accrued_low']-exit_cost)
                row['net_observed_high_usdc'] = str(pnl+a['accrued_high']-exit_cost)
                if not row['killed']:
                    row['net_week_low_usdc'] = str(pnl*604800/elapsed+a['low']/a['n']-exit_cost)
                    row['net_week_high_usdc'] = str(pnl*604800/elapsed+a['high']/a['n']-exit_cost)
    (folder/'report-data.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    lines = ['# BTC·ETH 거리별 가상 비교', '',
             '주문 0. 아래는 공개 체결 touch/through 대안 모형이며 실제 체결 확률·실제 수익이 아니다.', '',
             '| 마켓 | 거리 | 모형 | 가상 체결 | 주간 리워드 가정 범위 | 체결당 헷지 비용 USD | 관측 순손익 범위 USD | 7일 환산 순손익 범위 USD | 상태 |',
             '|---|---:|---|---:|---|---:|---|---|---|']
    def number(x): return f'{D(x):.4f}' if x is not None else '산출 불가'
    for r in data['rows']:
        reward = number(r['reward_scenario_week_usdc_mean'])+'~'+number(r['reward_scenario_week_usdc_high_mean'])
        observed = number(r['net_observed_low_usdc'])+'~'+number(r['net_observed_high_usdc'])
        weekly = number(r['net_week_low_usdc'])+'~'+number(r['net_week_high_usdc'])
        status = r['killed'] or ('헷지 자료 대기' if r['pending_hedge'] else '관측')
        lines.append(f"| {r['base']} | {r['distance_bps']} | {r['model']} | {r['fills']} | {reward} | {number(r['hedge_cost_per_fill_usd'])} | {observed} | {weekly} | {status} |")
    def span(rows, low, high=None):
        values = [r.get(k) for r in rows for k in ([low,high] if high else [low])]
        if any(v is None for v in values): return '산출 불가'
        a,b=min(D(v) for v in values),max(D(v) for v in values)
        return f'{a:.4f}' if a==b else f'{a:.4f}~{b:.4f}'
    compact = ['# 6개 조건 순이익 비교', '', 'touch/through 대안 모형 사이의 범위이며 실제 결과 보장이나 통계적 신뢰구간이 아니다.', '',
               '| 마켓 | 거리 bps | 가상 체결 건수 | 주간 리워드 가정 USDC | 체결당 헷지 비용 USD | 관측 순손익 USDC | 7일 환산 순손익 USDC |',
               '|---|---:|---|---|---|---|---|']
    for base in ('BTC','ETH'):
        for distance in (5,10,18):
            selected=[r for r in data['rows'] if r['base']==base and r['distance_bps']==distance]
            if len(selected)!=2: continue
            fills=str(min(r['fills'] for r in selected))+'~'+str(max(r['fills'] for r in selected))
            compact.append(f"| {base} | {distance} | {fills} | {span(selected,'reward_scenario_week_usdc_mean','reward_scenario_week_usdc_high_mean')} | {span(selected,'hedge_cost_per_fill_usd')} | {span(selected,'net_observed_low_usdc','net_observed_high_usdc')} | {span(selected,'net_week_low_usdc','net_week_high_usdc')} |")
    completed = data['state']=='finished' and data['updated_ts']>=data['deadline_ts']-.1
    low_key,high_key = ('net_observed_low_usdc','net_observed_high_usdc') if completed else ('net_week_low_usdc','net_week_high_usdc')
    compact += ['', '## 500 USD 자본의 주 2 USDC 순이익 기준', '',
                '같은 거리·같은 모형의 BTC+ETH만 합산한다. 서로 다른 거리 대안의 수익을 더하지 않는다. 주 2 USDC는 단순 연 환산 약 20.8%이며 수익 보장이 아니다.', '',
                '| 거리 bps | BTC+ETH 순이익 범위 USDC | 주 2 USDC 기준 |', '|---|---|---|']
    for distance in (5,10,18):
        totals=[]
        for model in ('touch','through'):
            selected=[r for r in data['rows'] if r['distance_bps']==distance and r['model']==model]
            if len(selected)!=2 or any(r.get(low_key) is None or r.get(high_key) is None for r in selected):
                totals=[];break
            totals.append((sum(D(r[low_key]) for r in selected),sum(D(r[high_key]) for r in selected)))
        if totals:
            low,high=min(x[0] for x in totals),max(x[1] for x in totals)
            verdict='가정 범위에서 기준 이상' if low>=2 else ('기준 미달' if high<2 else '모형 범위가 기준을 걸쳐 판단 유보')
            compact.append(f'| {distance} | {low:.4f}~{high:.4f} | {verdict} |')
        else:
            compact.append(f'| {distance} | 산출 불가 | 자료 부족/정지 가정 확인 |')
    compact += ['', '완료 시에는 7일 관측 순손익을 기준으로 비교하고, 그 전에는 주간 환산 가정만 표시한다. 실제 보상 지급·실주문 수익을 검증한 결과가 아니다.']
    lines = compact+['', '아래는 모형별 상세 표다.', '']+lines
    lines += ['', '순손익 = 평가 매매손익 + 가정 리워드 + 예상 펀딩 - 종료 정리 비용. 매매손익에는 maker 수수료가 이미 포함돼 중복 차감하지 않는다.',
              '거래소 간 가격 차이, 누적 대기, 300ms 후 가격 변화, Lighter 호가 스프레드·깊이, 수수료는 summary.json에 분리한다.',
              '가상 부분 체결은 Lighter 최소 주문을 넘을 때 묶어 헷지하므로 체결 건수와 헷지 건수가 다르다.',
              '순손익 산출 불가는 0 수익이 아니다. 정책 만료·잘린 호가·펀딩 공백·미확정 모형 헷지·정지 상태를 함께 확인한다.',
              '주간 환산은 관측기간과 같은 조건이 유지된다는 가정이다. 모형이 정지되면 계속 거래하는 환산을 하지 않는다. 실제 지급액은 별도 확인해야 한다.']
    (folder/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True)
    p.add_argument('--policy', default='docs/rewards-2026-10-27.json')
    p.add_argument('--duration', type=float, default=604800)
    p.add_argument('--no-rest', action='store_true', help='공개 WS 경로만 짧게 검증; 리워드 REST 관측 안 함')
    args = p.parse_args(argv)
    if not 0 < args.duration <= 604800: p.error('기간은 0 초과 604800 이하')
    asyncio.run(Watch(args.output, args.policy, time.time()+args.duration, args.no_rest).run())
    return 0


if __name__ == '__main__': raise SystemExit(main())
