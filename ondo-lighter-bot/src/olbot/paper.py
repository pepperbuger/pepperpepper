"""주문 없는 체결 시나리오. 가격/수량/손익은 Decimal, 시각만 float다.

공개 체결의 touch/through는 실제 자기 주문의 체결 보장이 아니다.
세 거리와 두 체결 모델은 서로 독립된 대안이며 동시에 자본을 사용하는 봇이 아니다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal as D, ROUND_DOWN, ROUND_CEILING, ROUND_FLOOR
import math
import itertools


def dec(value, *, zero=True):
    x = D(str(value))
    if not x.is_finite() or (x < 0 if zero else x <= 0):
        raise ValueError("가격/수량 오류")
    return x


def common_step(ondo_step: D, lighter_step: D) -> D:
    places = max(-ondo_step.as_tuple().exponent, -lighter_step.as_tuple().exponent, 0)
    scale = D(10) ** places
    return D(math.lcm(int(ondo_step * scale), int(lighter_step * scale))) / scale


def vwap(levels, size: D) -> D | None:
    if size <= 0:
        raise ValueError("양의 체결 수량 필요")
    left, total = size, D(0)
    for price, amount in levels:
        q = min(left, amount)
        total += q * price
        left -= q
        if left == 0:
            return total / size
    return None  # 깊이 부족을 마지막 가격/0 비용으로 메우지 않는다.


def parse_book(msg: dict):
    if msg.get('type') not in ('subscribed/order_book', 'update/order_book'):
        return None
    mid = int(msg['channel'].split(':')[1])
    book = msg['order_book']
    if int(book.get('code', 0)) != 0:
        raise ValueError('호가 응답 오류')
    sides = [[(dec(x['price'], zero=False), dec(x['size'])) for x in book[k]] for k in ('bids', 'asks')]
    return mid, msg['type'].startswith('subscribed/'), int(book['nonce']), book.get('begin_nonce'), sides


@dataclass
class Book:
    bids: dict = field(default_factory=dict)
    asks: dict = field(default_factory=dict)
    nonce: int | None = None
    ts: float = 0

    def clear(self):
        self.bids.clear(); self.asks.clear(); self.nonce = None; self.ts = 0

    def apply(self, snapshot, nonce, begin, sides, ts):
        if not snapshot and (self.nonce is None or begin is None or int(begin) != self.nonce):
            self.clear()
            raise ValueError('Lighter 호가 nonce 불연속: 재구독 필요')
        if snapshot:
            self.clear()
        for dest, updates in zip((self.bids, self.asks), sides):
            for p, s in updates:
                if s == 0:
                    dest.pop(p, None)
                else:
                    dest[p] = s
        self.nonce, self.ts = nonce, ts
        if self.bids and self.asks and max(self.bids) >= min(self.asks):
            self.clear()
            raise ValueError('Lighter 역전 호가')

    @property
    def mid(self):
        return (max(self.bids) + min(self.asks)) / 2 if self.bids and self.asks else None

    def execute(self, signed_ondo_size):
        # Ondo 매수의 반대 헷지는 Lighter 매도다.
        side = sorted(self.bids.items(), reverse=True) if signed_ondo_size > 0 else sorted(self.asks.items())
        return vwap(side, abs(signed_ondo_size))


def parse_trades(msg):
    if msg.get('type') != 'update' or msg.get('channel') != 'tradesPerps':
        return []
    out = []
    for t in msg['data']:
        if t['aggressor_side'] not in ('buy', 'sell') or not isinstance(t['id'], str):
            raise ValueError('공개 체결 방향/ID 오류')
        stamp = datetime.fromisoformat(t['time'].replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('공개 체결 시각 시간대 누락')
        out.append((t['market'].split('-')[0], t['id'], t['aggressor_side'],
                    dec(t['price'], zero=False), dec(t['size'], zero=False), stamp.timestamp()))
    return out


def parse_ondo_funding(msg):
    if msg.get('type') != 'update' or msg.get('channel') != 'fundingRatesPerps': return []
    out = []
    for row in msg['data']:
        rate = D(row['rate'])
        if not rate.is_finite(): raise ValueError('Ondo funding invalid')
        out.append((row['market'].split('-')[0], rate))
    return out


def parse_lighter_funding(msg):
    if msg.get('type') not in ('subscribed/market_stats', 'update/market_stats'): return None
    row = msg['market_stats']
    rate = D(str(row['current_funding_rate'])) / 100
    if not rate.is_finite(): raise ValueError('Lighter funding invalid')
    return int(row['market_id']), rate


def reward_fraction(distance: D):
    # 공식 공지는 0~5 / 6~10 / 11~25로만 표시한다. 소수 경계 공백은 추측하지 않는다.
    if D(0) <= distance <= D(5): return D('.85')
    if D(6) <= distance <= D(10): return D('.10')
    if D(11) <= distance <= D(25): return D('.05')
    return None


def reward_scenario(book, orders, pool: D):
    """동일 버킷의 실제 자기 호가만 분자에 넣는다. 잘린 분모는 확정하지 않는다."""
    bids, asks = [[(dec(p, zero=False), dec(s)) for p, s in book[k]] for k in ('bids', 'asks')]
    if not bids or not asks:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2
    if bids[0][0] >= asks[0][0]: return None
    if any(len(side) >= 100 and abs(side[-1][0] / mid - 1) * 10000 < 25 for side in (bids, asks)):
        return None
    competition = {D('.85'): D(0), D('.10'): D(0), D('.05'): D(0)}
    uncertain = dict.fromkeys(competition, D(0))
    ambiguous = False
    for price, size in bids + asks:
        f = reward_fraction(abs(price / mid - 1) * 10000)
        if f is not None:
            competition[f] += price * size
        elif abs(price / mid - 1) * 10000 <= 25:
            ambiguous = True
            d = abs(price / mid - 1) * 10000
            for f in ((D('.85'), D('.10')) if d < 6 else (D('.10'), D('.05'))):
                uncertain[f] += price * size
    options = []
    for price, size in orders:
        d = abs(price / mid - 1) * 10000
        f = reward_fraction(d)
        if f is not None: choices = (f,)
        elif 5 < d < 6: choices = (D('.85'), D('.10'))
        elif 10 < d < 11: choices = (D('.10'), D('.05'))
        else: choices = (None,)
        ambiguous |= len(choices) > 1
        options.append([(c, price * size) for c in choices])
    lows, highs = [], []
    for allocation in itertools.product(*options):
        own = dict.fromkeys(competition, D(0))
        for f, amount in allocation:
            if f is not None: own[f] += amount
        lows.append(sum((pool*f*a/(competition[f]+uncertain[f]+a) for f,a in own.items() if a), D(0)))
        highs.append(sum((pool*f*a/(competition[f]+a) for f,a in own.items() if a), D(0)))
    return min(lows), max(highs), ambiguous


@dataclass
class VirtualOrder:
    price: D
    remaining: D
    active_at: float


@dataclass
class Scenario:
    base: str
    distance: int
    model: str
    tick: D
    step: D
    min_quote: D
    min_base: D
    orders: dict = field(default_factory=dict)
    last_quote: float = 0
    pos: D = D(0)
    lpos: D = D(0)
    cash: D = D(0)
    maker_fees: D = D(0)
    fills: int = 0
    fill_notional: D = D(0)
    hedges: int = 0
    costed_fills: int = 0
    costed_fees: D = D(0)
    pending: dict | None = None
    unmatched_value: D = D(0)
    unmatched_ref: D = D(0)
    basis_cost: D = D(0)
    accumulation_cost: D = D(0)
    delay_cost: D = D(0)
    spread_cost: D = D(0)
    delayed_seconds: list = field(default_factory=list)
    failed_hedges: int = 0
    killed: str | None = None
    exposed_since: float | None = None
    funding_estimate: D = D(0)
    funding_unknown_seconds: float = 0
    last_tick: float = 0
    eligible_seconds: float = 0
    data_gap_seconds: float = 0
    reward_values: list = field(default_factory=list)
    reward_high_values: list = field(default_factory=list)
    reward_accrued_low: D = D(0)
    reward_accrued_high: D = D(0)
    reward_unknown: int = 0
    quote_edge: D = D(0)

    def quote(self, mid, bid, ask, now, healthy=True, can_increase=True):
        if self.killed or not healthy:
            self.orders.clear()
            return
        if self.last_quote and now - self.last_quote < 10:
            return
        self.last_quote = now
        self.orders.clear()
        for side, sign in (('buy', -1), ('sell', 1)):
            # 경계 5/10bps는 안쪽 한 tick으로 둔다. 실제 거리로 보상 버킷을 다시 판정한다.
            target = mid * (1 + D(sign * self.distance) / 10000)
            price = (target / self.tick).to_integral_value(rounding=ROUND_CEILING if side == 'buy' else ROUND_FLOOR) * self.tick
            size = (D(60) / price / self.step).to_integral_value(rounding=ROUND_DOWN) * self.step
            if size < self.min_base or size * price < self.min_quote * D('1.05'):
                continue
            if (side == 'buy' and price >= ask) or (side == 'sell' and price <= bid):
                continue  # post-only가 될 수 없는 가상 호가를 만들지 않는다.
            if abs((self.pos + (size if side == 'buy' else -size)) * mid) > 200:
                continue
            if not can_increase and abs(self.pos + (size if side == 'buy' else -size)) > abs(self.pos):
                continue
            self.orders[side] = VirtualOrder(price, size, now + 2)  # 취소+생성/대기 공백 가정

    def trade(self, aggressor, price, size, ts, recv_ts, omid, healthy):
        if self.killed or not healthy or not -1 <= recv_ts-ts <= 2:
            return None
        side = 'buy' if aggressor == 'sell' else 'sell'
        order = self.orders.get(side)
        if order is None or order.active_at > ts:
            return None
        crossed = price < order.price if side == 'buy' else price > order.price
        touched = price <= order.price if side == 'buy' else price >= order.price
        if not (crossed if self.model == 'through' else touched):
            return None
        q = (min(size, order.remaining) / self.step).to_integral_value(rounding=ROUND_DOWN) * self.step
        if q <= 0: return None
        signed = q if side == 'buy' else -q
        value = signed * order.price
        fee = q * order.price * D('.0001')
        self.pos += signed
        self.cash -= value + fee
        self.maker_fees += fee
        self.quote_edge += signed * (order.price - omid)
        self.unmatched_value += value
        self.unmatched_ref += signed * omid
        self.fills += 1
        self.fill_notional += abs(value)
        order.remaining -= q
        if order.remaining == 0: self.orders.pop(side)
        return {'side': side, 'size': str(q), 'price': str(order.price), 'maker_fee': str(fee)}

    def advance(self, now, omid, book, healthy, orate=None, lrate=None):
        dt = max(now - self.last_tick, 0) if self.last_tick else 0
        if dt > 2:
            healthy = False
        self.last_tick = now
        if healthy:
            self.eligible_seconds += dt
        else:
            self.data_gap_seconds += dt
            self.orders.clear()
        if omid is not None and book.mid is not None and (self.pos or self.lpos):
            if orate is None or lrate is None:
                self.funding_unknown_seconds += dt
            else:
                # 공개 예상 시간당 비율을 연속 시간으로 적분한 근사. 실제 정시 지급이 아니다.
                self.funding_estimate += -(self.pos * omid * orate + self.lpos * book.mid * lrate) * D(str(dt)) / 3600
        delta = self.pos + self.lpos
        self.exposed_since = (self.exposed_since or now) if omid and abs(delta * omid) > 40 else None
        if self.exposed_since is not None and now - self.exposed_since > 20:
            self.killed = self.killed or 'unhedged_40_usd_20sec'
        if self.pending:
            p = self.pending
            if healthy and book.ts >= p['due']:
                execution = book.execute(p['signed'])
                if execution is not None:
                    self.cash += p['signed'] * execution  # Standard taker 수수료 0 (TWAP 아님)
                    self.lpos -= p['signed']
                    self.basis_cost += p['signed'] * (p['omid'] - p['lmid'])
                    self.accumulation_cost += p['ref'] - p['signed'] * p['omid']
                    self.delay_cost += p['signed'] * (p['lmid'] - book.mid)
                    self.spread_cost += p['signed'] * (book.mid - execution)
                    self.delayed_seconds.append(book.ts - p['sent'])
                    self.hedges += 1
                    self.costed_fills += p['fill_count']
                    self.costed_fees += p['fees']
                    self.pending = None
                    return {'signed_size': str(p['signed']), 'execution': str(execution),
                            'delay_sec': book.ts-p['sent'], 'basis_usd': str(p['signed']*(p['omid']-p['lmid'])),
                            'delay_usd': str(p['signed']*(p['lmid']-book.mid)),
                            'spread_usd': str(p['signed']*(book.mid-execution))}
            if now - p['due'] > 2:
                if not p.get('failed'):
                    self.failed_hedges += 1
                    p['failed'] = True
                self.killed = self.killed or 'hedge_book_missing_after_300ms'
                # 알 수 없는 체결을 0 비용으로 처리하지 않는다. 새 가상 헷지도 차단한다.
            return None
        if healthy and delta and abs(delta) >= self.min_base and abs(delta) * book.mid >= self.min_quote * D('1.05'):
            self.pending = {'signed': delta, 'sent': now, 'due': now+.300, 'lmid': book.mid, 'omid': omid,
                            'ref': self.unmatched_ref, 'value': self.unmatched_value,
                            'fill_count': self.fills-self.costed_fills, 'fees': self.maker_fees-self.costed_fees}
            self.unmatched_ref = self.unmatched_value = D(0)
        if self.killed: self.orders.clear()
        return None

    def result(self, omid, book, now):
        marked = self.cash + self.pos * omid + self.lpos * book.mid if omid and book.mid else None
        costs = self.basis_cost + self.delay_cost + self.spread_cost + self.accumulation_cost + self.costed_fees
        # 출구 taker 비용과 스프레드/깊이까지 별도 추정해 열린 포지션을 공짜로 정리하지 않는다.
        exit_cost = None
        if omid and book.mid and self.pos + self.lpos == 0:
            exit_cost = abs(self.pos * omid) * D('.00025')
        return {'base': self.base, 'distance_bps': self.distance, 'model': self.model,
                'fills': self.fills, 'hedges': self.hedges, 'fill_notional_usd': str(self.fill_notional),
                'maker_fees_usd': str(self.maker_fees), 'basis_cost_usd': str(self.basis_cost),
                'accumulation_cost_usd': str(self.accumulation_cost), 'delay_cost_usd': str(self.delay_cost),
                'spread_depth_cost_usd': str(self.spread_cost), 'hedge_cost_per_fill_usd': str(costs/self.costed_fills) if self.costed_fills else None,
                'costed_fills': self.costed_fills,
                'mark_pnl_ex_reward_funding_usd': str(marked) if marked is not None else None,
                'funding_estimate_usd': str(self.funding_estimate), 'funding_unknown_seconds': self.funding_unknown_seconds,
                'exit_fee_only_usd': str(exit_cost) if exit_cost is not None else None,
                'reward_scenario_week_usdc_mean': str(sum(self.reward_values)/len(self.reward_values)) if self.reward_values else None,
                'reward_scenario_week_usdc_high_mean': str(sum(self.reward_high_values)/len(self.reward_high_values)) if self.reward_high_values else None,
                'reward_samples': len(self.reward_values), 'reward_unknown_samples': self.reward_unknown,
                'position': str(self.pos), 'lighter_position': str(self.lpos), 'pending_hedge': self.pending is not None,
                'qualified_data_seconds': self.eligible_seconds, 'data_gap_seconds': self.data_gap_seconds,
                'killed': self.killed, 'failed_hedges': self.failed_hedges,
                'mean_observed_delay_sec': sum(self.delayed_seconds)/len(self.delayed_seconds) if self.delayed_seconds else None}
