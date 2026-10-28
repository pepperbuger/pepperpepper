"""마켓별로 `Lighter 포지션 = -Ondo 포지션`을 맞춘다.

포지션 차이를 기준으로 헷지하되, 앞선 주문 결과가 미확정이면 추가 주문을 막는다.
미확정은 전송 전에 저장하고, 시간 경과나 재시작으로 해제하지 않는다.

미확정 해제 근거 (둘 중 하나):
1. 주문 최종 상태: 전송 전에 정한 client_order_index로 Lighter accountOrders를 조회해서
   filled 또는 canceled-* 이면 그 주문의 체결 수량이 확정된다. 포지션이 그 수량만큼 반영된 것까지
   확인한 뒤 해제한다. 일부 체결이나 미체결(가격 초과 취소 등)도 이 방법으로 확정되어 남은 양을 다시 헷지한다.
2. 포지션 전량 반영: 주문 번호가 없는 예전 기록은 주문 수량 전체가 포지션에 반영됐을 때만 해제한다.
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
    client_order_index: int | None = None  # 없으면(예전 기록) 포지션 전량 반영으로만 해제

    def expected_move(self) -> Decimal:
        return -self.size if self.is_sell else self.size

    def signed(self, qty: Decimal) -> Decimal:
        return -qty if self.is_sell else qty


@dataclass
class HedgeState:
    ondo_pos: Decimal = Decimal(0)
    lighter_pos: Decimal = Decimal(0)
    unhedged_since: float | None = None
    consecutive_failures: int = 0
    last_attempt_ts: float = 0.0
    pending: PendingHedge | None = None
    # 직전 대사에서 확정된 주문의 최종 상태. 체결 없이/일부만 끝났으면 다음 회차를 실패로 세고 가격을 넓힌다.
    last_final_status: str | None = None

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
        if p.client_order_index is not None and hasattr(self.trader, "order_status"):
            return await self._resolve_by_order_status(base, p)
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

    async def _resolve_by_order_status(self, base: str, p: PendingHedge) -> str | None:
        st = self.states[base]
        m = self.markets[base]
        try:
            os = await self.trader.order_status(p.client_order_index)
        except Exception as e:
            log.warning("hedge %s order status lookup failed, keeping unconfirmed: %r", base, e)
            return WAITING
        if os is None:
            # 거래소에 아직 없음: 반영 전일 수도 있어서 미체결의 증거가 아니다
            log.info("hedge %s order %s not found yet, keeping unconfirmed", base, p.client_order_index)
            return WAITING
        if not os.terminal:
            return WAITING
        sent_size = Decimal(m.to_int_size(p.size)).scaleb(-m.size_decimals)  # 거래소에 실제로 보낸 수량(내림)
        if os.initial != sent_size:
            # 같은 주문 번호인데 수량이 다르면 다른 주문이거나 응답이 잘못됐다. 확정하지 않는다.
            log.error("hedge %s order %s size mismatch: exchange %s vs sent %s; keeping unconfirmed",
                      base, p.client_order_index, os.initial, sent_size)
            return WAITING
        # 최종 체결 수량이 확정됐다. 포지션이 그만큼 반영된 것을 확인한 뒤에만 해제한다.
        # 그 전에 해제하면 오래된 포지션으로 차이를 계산해 중복 헷지할 수 있다.
        try:
            await self.refresh_lighter()
        except Exception as e:
            log.warning("hedge %s final status %s known but position refresh failed: %r", base, os.status, e)
            return WAITING
        expected = p.lighter_pos_before + p.signed(os.filled)
        # 오차를 허용하지 않는다. 최소 단위 하나(예: 0.01개)라도 반영 전이면 기다린다.
        if st.lighter_pos != expected:
            log.warning("hedge %s order %s is %s (filled %s) but position %s != expected %s; waiting",
                        base, p.client_order_index, os.status, os.filled, st.lighter_pos, expected)
            return WAITING
        log.info("hedge %s order %s final: %s filled %s/%s", base, p.client_order_index, os.status, os.filled, p.size)
        if self.record:
            self.record(base, p.size, p.is_sell, None, None, os.filled, f"final:{os.status}")
        self._set_pending(base, None)
        st.last_final_status = os.status
        return None

    async def _hedge_attempts(self, base: str, lighter_mid: Decimal) -> str:
        st = self.states[base]
        m = self.markets[base]
        waiting = await self._resolve_pending(base)
        if waiting:
            return waiting
        if st.last_final_status not in (None, "filled"):
            # 앞선 대사에서 넘어온 주문이 체결 없이/일부만 끝났다: 그 회차를 실패로 센다
            st.consecutive_failures += 1
            log.warning("hedge %s previous order ended %s (counted as failure %d)",
                        base, st.last_final_status, st.consecutive_failures)
        st.last_final_status = None
        # 확실히 거절됐거나 미체결로 확정된 직전 회차가 있으면 더 넓은 가격부터 시작한다.
        widen = min(st.consecutive_failures, self.retries - 1)
        for attempt in range(self.retries):
            plan = hedge_order(st.delta, m, lighter_mid, self.slippage_bps * (attempt + 1 + widen))
            if plan is None:
                return DONE
            size, is_sell, worst = plan
            before = st.lighter_pos
            st.last_attempt_ts = time.time()
            coi = self.trader.new_client_order_index() if hasattr(self.trader, "new_client_order_index") else None
            # 이 순간부터 결과를 확인할 때까지는 체결됐을 수도 있는 주문으로 다룬다 (주문 번호와 함께 먼저 저장)
            self._set_pending(base, PendingHedge(size, is_sell, before, st.last_attempt_ts, coi))
            try:
                if coi is None:
                    rejected = await self.trader.market_order(m, size, is_sell, worst)
                else:
                    rejected = await self.trader.market_order(m, size, is_sell, worst, client_order_index=coi)
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
            if self.record:
                self.record(base, size, is_sell, worst, lighter_mid, "pending", unknown_error)
            await asyncio.sleep(1.5)  # 첫 조회 전 대기. 최종 체결/미체결 보장 시간은 아니다.
            if await self._resolve_pending(base) == WAITING:
                return WAITING  # 결과를 모르면 다시 보내지 않는다
            st.last_final_status = None  # 이번 호출 안에서 확정된 결과는 아래 회차 번호로 가격을 넓힌다
            # 최종 결과 확인 완료 (전량, 일부, 또는 미체결). 남은 차이가 있으면 다음 회차에서 넓은 가격으로 보낸다.
            if hedge_order(st.delta, m, lighter_mid, self.slippage_bps) is None:
                return DONE
        return FAILED
