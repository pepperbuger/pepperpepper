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


# 결과를 모르는 주문(응답 유실, 또는 접수됐는데 포지션 반영이 아직 없음)을 이 시간 동안은 체결된 것으로 본다.
# 그동안 같은 마켓에 새 헷지 주문을 보내지 않는다. 지나도 포지션이 안 움직이면 미체결로 보고 다시 보낸다.
PENDING_CONFIRM_SEC = 10.0

DONE, FAILED, WAITING = "done", "failed", "waiting"


@dataclass
class PendingHedge:
    size: Decimal
    is_sell: bool
    lighter_pos_before: Decimal
    sent_ts: float

    def expected_move(self) -> Decimal:
        return -self.size if self.is_sell else self.size


@dataclass
class HedgeState:
    ondo_pos: Decimal = Decimal(0)
    lighter_pos: Decimal = Decimal(0)
    unhedged_since: float | None = None
    consecutive_failures: int = 0
    last_attempt_ts: float = 0.0
    pending: PendingHedge | None = None

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


def pending_confirmed(p: PendingHedge, lighter_pos: Decimal) -> bool:
    """포지션이 주문 방향으로 절반 이상 움직였으면 그 주문이 체결된 것으로 본다."""
    moved = lighter_pos - p.lighter_pos_before
    return moved * p.expected_move() > 0 and abs(moved) * 2 >= p.size


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
        """한 마켓의 차이를 헷지한다.

        예외를 밖으로 내보내지 않는다. 결과가 확실히 실패면 consecutive_failures를 올리고(3이면 kill),
        결과를 아직 모르는 주문이 있으면(WAITING) 아무것도 보내지 않고 다음 대사를 기다린다.
        kill 뒤에도 헷지는 계속되어야 해서 이 함수는 절대 죽지 않는다.
        """
        if lighter_mid is None:
            return
        async with self.lock:
            st = self.states[base]
            try:
                outcome = await self._hedge_attempts(base, lighter_mid)
            except Exception as e:
                log.exception("hedge %s unexpected error: %r", base, e)
                outcome = FAILED
            if outcome == DONE:
                st.consecutive_failures = 0
            elif outcome == FAILED:
                st.consecutive_failures += 1
                log.error("hedge %s failed (consecutive %d), delta=%s", base, st.consecutive_failures, st.delta)

    async def _resolve_pending(self, base: str) -> str | None:
        """확인 대기 중인 주문을 정리한다. 아직 기다려야 하면 WAITING, 정리됐으면 None."""
        st = self.states[base]
        p = st.pending
        if p is None:
            return None
        try:
            await self.refresh_lighter()
        except Exception as e:
            log.warning("hedge %s pending check: position refresh failed: %r", base, e)
            return WAITING
        if pending_confirmed(p, st.lighter_pos):
            log.info("hedge %s unconfirmed order now reflected in position", base)
            st.pending = None
            return None
        if time.time() - p.sent_ts < PENDING_CONFIRM_SEC:
            return WAITING
        log.warning("hedge %s unconfirmed order not reflected after %.0fs, treating as unfilled", base, PENDING_CONFIRM_SEC)
        st.pending = None
        st.consecutive_failures += 1
        return None

    async def _hedge_attempts(self, base: str, lighter_mid: Decimal) -> str:
        st = self.states[base]
        m = self.markets[base]
        waiting = await self._resolve_pending(base)
        if waiting:
            return waiting
        # 직전 회차들이 실패했으면(미체결로 만료 포함) 더 넓은 가격부터 시작한다
        widen = min(st.consecutive_failures, self.retries - 1)
        for attempt in range(self.retries):
            plan = hedge_order(st.delta, m, lighter_mid, self.slippage_bps * (attempt + 1 + widen))
            if plan is None:
                return DONE
            size, is_sell, worst = plan
            before = st.lighter_pos
            st.last_attempt_ts = time.time()
            # 이 순간부터 결과를 확인할 때까지는 체결됐을 수도 있는 주문으로 다룬다
            st.pending = PendingHedge(size, is_sell, before, st.last_attempt_ts)
            try:
                rejected = await self.trader.market_order(m, size, is_sell, worst)
            except Exception as e:
                rejected = None  # 응답 유실: 거래소에 닿았는지 모른다
                log.warning("hedge %s send outcome unknown: %r", base, e)
                unknown_error = f"unknown: {e!r}"
            else:
                unknown_error = ""
            if rejected:
                # 거래소가 거절했거나 서명 단계에서 실패: 확실히 체결 안 됨
                st.pending = None
                log.warning("hedge %s attempt %d rejected: %s", base, attempt + 1, rejected)
                if self.record:
                    self.record(base, size, is_sell, worst, lighter_mid, Decimal(0), rejected)
                continue
            await asyncio.sleep(1.5)  # IOC라 이 사이에 체결되거나 사라진다
            try:
                await self.refresh_lighter()
            except Exception as e:
                log.warning("hedge %s position refresh failed, keeping order unconfirmed: %r", base, e)
                if self.record:
                    self.record(base, size, is_sell, worst, lighter_mid, "unknown", unknown_error or "refresh_failed")
                return WAITING
            filled = st.lighter_pos - before
            if self.record:
                self.record(base, size, is_sell, worst, lighter_mid, abs(filled), unknown_error)
            if not pending_confirmed(st.pending, st.lighter_pos):
                # 접수됐을 수 있는데 아직 포지션에 안 보인다: 다시 보내지 않고 기다린다
                return WAITING
            st.pending = None
            if hedge_order(st.delta, m, lighter_mid, self.slippage_bps) is None:
                return DONE
        return FAILED
