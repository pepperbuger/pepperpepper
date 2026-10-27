"""마켓별로 `Lighter 포지션 = -Ondo 포지션`을 맞춘다.

포지션 차이를 기준으로 헷지하되, 앞선 주문 결과가 미확정이면 추가 주문을 막는다.
미확정은 전송 전에 저장하고, 시간 경과나 재시작으로 해제하지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

from olbot.lighter.client import LighterMarket

log = logging.getLogger("olbot.hedger")


# 호가 일시정지까지 기다릴 시간. 이 시간이 지나도 미확정 주문은 만료되지 않는다.
PENDING_PAUSE_SEC = 10.0

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
    """주문 수량 전체가 같은 방향으로 반영됐을 때만 대기를 해제한다.

    일부 체결이나 무변화는 주문의 최종 결과를 알려주지 않는다. 계정의 해당 마켓을
    이 봇만 거래한다는 전제이며, 외부 주문/수동 거래가 있으면 별도 주문 대사가 필요하다.
    """
    moved = lighter_pos - p.lighter_pos_before
    return moved * p.expected_move() > 0 and abs(moved) >= p.size


@dataclass
class Hedger:
    trader: object  # LighterTrader 또는 테스트용 가짜
    markets: dict[str, LighterMarket]  # base -> Lighter 마켓
    slippage_bps: float
    retries: int
    record: object = None  # ledger.record_hedge 같은 콜백 (선택)
    states: dict[str, HedgeState] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    save_pending: Callable[[str, PendingHedge | None], None] | None = None
    initial_pending: dict[str, PendingHedge] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if set(self.initial_pending) - set(self.markets):
            raise ValueError("저장된 미확정 헷지 마켓이 설정에 없다. 원래 마켓을 복원하고 주문 상태를 확인한다")
        for base in self.markets:
            self.states.setdefault(base, HedgeState())
            if base in self.initial_pending:
                self.states[base].pending = self.initial_pending[base]

    def _set_pending(self, base: str, pending: PendingHedge | None) -> None:
        # 반드시 주문 전송 전에 커밋한다. 저장 실패 시 주문을 보내지 않는다.
        if self.save_pending is not None:
            self.save_pending(base, pending)
        self.states[base].pending = pending

    def overdue_pending(self, now: float) -> tuple[str, ...]:
        return tuple(sorted(base for base, st in self.states.items()
                            if st.pending is not None and now - st.pending.sent_ts >= PENDING_PAUSE_SEC))

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
            self._set_pending(base, None)
            return None
        # 시간 경과도, 조회 성공 시 무변화도 미체결의 증거가 아니다.
        # pending을 유지하고 위험/호가 루프가 일시정지하도록 한다.
        return WAITING

    async def _hedge_attempts(self, base: str, lighter_mid: Decimal) -> str:
        st = self.states[base]
        m = self.markets[base]
        waiting = await self._resolve_pending(base)
        if waiting:
            return waiting
        # 확실히 거절된 직전 회차가 있으면 더 넓은 가격부터 시작한다.
        widen = min(st.consecutive_failures, self.retries - 1)
        for attempt in range(self.retries):
            plan = hedge_order(st.delta, m, lighter_mid, self.slippage_bps * (attempt + 1 + widen))
            if plan is None:
                return DONE
            size, is_sell, worst = plan
            before = st.lighter_pos
            st.last_attempt_ts = time.time()
            # 이 순간부터 결과를 확인할 때까지는 체결됐을 수도 있는 주문으로 다룬다
            self._set_pending(base, PendingHedge(size, is_sell, before, st.last_attempt_ts))
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
                self._set_pending(base, None)
                log.warning("hedge %s attempt %d rejected: %s", base, attempt + 1, rejected)
                if self.record:
                    self.record(base, size, is_sell, worst, lighter_mid, Decimal(0), rejected)
                continue
            await asyncio.sleep(1.5)  # 첫 조회 전 대기. 최종 체결/미체결 보장 시간은 아니다.
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
            self._set_pending(base, None)
            if hedge_order(st.delta, m, lighter_mid, self.slippage_bps) is None:
                return DONE
        return FAILED
