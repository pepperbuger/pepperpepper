"""킬 스위치 판단 (순수 로직). 실제 조치(호가 전부 취소, 신규 호가 중단)는 엔진이 한다.

kill: 프로세스를 다시 시작하기 전까지 호가를 걸지 않는다. 헷지는 계속한다(노출을 줄이는 쪽이라서).
pause: 원인이 사라지면 자동으로 호가를 다시 건다 (예: WS 잠깐 끊김).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class RiskInputs:
    now: float
    unhedged_usd: dict[str, Decimal]
    unhedged_since: dict[str, float | None]
    hedge_failures: dict[str, int]
    public_ws_silent_sec: float
    private_ws_up: bool
    equity_start: Decimal | None
    equity_now: Decimal | None
    ondo_margin_usage: float | None


@dataclass
class RiskDecision:
    kill_reason: str | None = None
    pause_reason: str | None = None


def evaluate(inp: RiskInputs, max_unhedged_usd: Decimal, max_unhedged_sec: float, ws_stale_sec: float,
             daily_loss_limit_usd: Decimal, max_margin_usage: float) -> RiskDecision:
    for base, usd in inp.unhedged_usd.items():
        since = inp.unhedged_since.get(base)
        if usd > max_unhedged_usd and since is not None and inp.now - since > max_unhedged_sec:
            return RiskDecision(kill_reason=f"{base} 헷지 안 된 노출 {usd:.0f} USD가 {inp.now - since:.0f}초 지속")
        if inp.hedge_failures.get(base, 0) >= 3:
            return RiskDecision(kill_reason=f"{base} 헷지 연속 실패 {inp.hedge_failures[base]}회")
    if inp.equity_start is not None and inp.equity_now is not None:
        loss = inp.equity_start - inp.equity_now
        if loss > daily_loss_limit_usd:
            return RiskDecision(kill_reason=f"손실 {loss:.2f} USD가 한도 {daily_loss_limit_usd} 초과")
    if inp.ondo_margin_usage is not None and inp.ondo_margin_usage > max_margin_usage:
        return RiskDecision(pause_reason=f"Ondo 증거금 사용률 {inp.ondo_margin_usage:.0%}")
    if not inp.private_ws_up:
        return RiskDecision(pause_reason="Ondo 비공개 WS 끊김")
    if inp.public_ws_silent_sec > ws_stale_sec:
        return RiskDecision(pause_reason=f"시세 WS 무응답 {inp.public_ws_silent_sec:.0f}초")
    return RiskDecision()
