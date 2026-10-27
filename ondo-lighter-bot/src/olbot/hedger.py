"""마켓별로 `Lighter 포지션 = -Ondo 포지션`을 맞춘다.

체결 추적 대신 포지션 차이를 기준으로 한다. 체결 이벤트를 놓치거나 중복으로 받아도
다음 대사에서 실제 포지션 차이만큼만 헷지하므로 결과가 틀어지지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal

from olbot.lighter.client import LighterMarket

log = logging.getLogger("olbot.hedger")


@dataclass
class HedgeState:
    ondo_pos: Decimal = Decimal(0)
    lighter_pos: Decimal = Decimal(0)
    unhedged_since: float | None = None
    consecutive_failures: int = 0
    last_attempt_ts: float = 0.0

    @property
    def delta(self) -> Decimal:
        """양수면 전체가 롱으로 쏠림 -> Lighter에서 팔아야 한다."""
        return self.ondo_pos + self.lighter_pos


def hedge_order(delta: Decimal, m: LighterMarket, mid: Decimal, slippage_bps: float) -> tuple[Decimal, bool, Decimal] | None:
    """delta를 없애는 주문 (수량, 매도 여부, 최악 가격). 최소 주문보다 작으면 None."""
    size = abs(delta)
    if size < m.min_hedge_size(mid):
        return None
    is_sell = delta > 0
    slip = Decimal(str(slippage_bps)) / Decimal(10_000)
    worst = mid * (1 - slip) if is_sell else mid * (1 + slip)
    return size, is_sell, worst


@dataclass
class Hedger:
    trader: object  # LighterTrader 또는 테스트용 가짜
    markets: dict[str, LighterMarket]  # base -> Lighter 마켓
    slippage_bps: float
    retries: int
    record: object = None  # ledger.record_hedge 같은 콜백 (선택)
    states: dict[str, HedgeState] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        for base in self.markets:
            self.states.setdefault(base, HedgeState())

    def set_ondo_positions(self, by_base: dict[str, Decimal], skip: set[str] | None = None) -> None:
        """REST 스냅샷으로 덮어쓴다. 방금 체결된 마켓(skip)은 스냅샷이 체결 전 값일 수 있어 건너뛴다."""
        for base, st in self.states.items():
            if skip and base in skip:
                continue
            st.ondo_pos = by_base.get(base, Decimal(0))

    async def refresh_lighter(self) -> None:
        pos = await self.trader.positions()
        for base, st in self.states.items():
            st.lighter_pos = pos.get(self.markets[base].market_id, Decimal(0))

    def unhedged_usd(self, base: str, mid: Decimal) -> Decimal:
        return abs(self.states[base].delta) * mid

    async def hedge(self, base: str, lighter_mid: Decimal | None) -> None:
        """한 마켓의 차이를 헷지한다. 실패하면 슬리피지를 넓혀 retries번 다시 시도한다."""
        if lighter_mid is None:
            return
        async with self.lock:
            st = self.states[base]
            m = self.markets[base]
            for attempt in range(self.retries):
                plan = hedge_order(st.delta, m, lighter_mid, self.slippage_bps * (attempt + 1))
                if plan is None:
                    st.consecutive_failures = 0
                    return
                size, is_sell, worst = plan
                before = st.lighter_pos
                st.last_attempt_ts = time.time()
                err = await self.trader.market_order(m, size, is_sell, worst)
                await asyncio.sleep(1.5)  # 체결 반영 대기 후 실제 포지션으로 확인
                try:
                    await self.refresh_lighter()
                except Exception as e:
                    # 체결 여부를 모르는 채로 재시도하면 두 번 주문할 수 있다. 다음 대사에 맡긴다.
                    log.warning("lighter position refresh failed, retry deferred to next reconcile: %r", e)
                    if self.record:
                        self.record(base, size, is_sell, worst, lighter_mid, "unknown", err or "refresh_failed")
                    return
                filled = abs(st.lighter_pos - before)
                if self.record:
                    self.record(base, size, is_sell, worst, lighter_mid, filled, err or "")
                if err:
                    log.warning("hedge %s attempt %d rejected: %s", base, attempt + 1, err)
                if err is None and filled > 0:
                    st.consecutive_failures = 0
                    if hedge_order(st.delta, m, lighter_mid, self.slippage_bps) is None:
                        return
            st.consecutive_failures += 1
            log.error("hedge %s failed after %d attempts, delta=%s", base, self.retries, st.delta)
