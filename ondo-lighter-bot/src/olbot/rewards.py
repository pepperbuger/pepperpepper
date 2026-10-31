"""공식 공지를 사람이 확인한 주간 정책. 임의 마켓으로 자동 전환하지 않는다."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class RewardPolicy:
    source: str
    starts: float
    ends: float  # exclusive UTC boundary
    allowed_bases: frozenset[str]
    per_market_usdc: Decimal

    def block_reason(self, bases: list[str], now: float) -> str | None:
        if not self.starts <= now < self.ends:
            return "유동성 리워드 주간 정책 유효기간 밖: 새 공식 공지 확인 필요"
        excluded = sorted(set(bases) - self.allowed_bases)
        if excluded:
            return "유동성 리워드 확인 대상 아님: " + ",".join(excluded)
        return None


def load_policy(path: str | Path) -> RewardPolicy:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    # UTC offset 필수. PC 현지 시간에 따라 정책 경계가 변하지 않는다.
    dates = [datetime.fromisoformat(raw[k]) for k in ("starts_utc", "ends_utc")]
    if any(d.tzinfo is None for d in dates) or dates[0] >= dates[1]:
        raise ValueError("리워드 정책의 기간/시간대 오류")
    bases = raw["allowed_bases"]
    if not isinstance(bases, list) or not bases or any(not isinstance(b, str) or not b or b != b.upper() for b in bases):
        raise ValueError("리워드 정책 대상 목록 오류")
    pool = Decimal(raw["per_market_usdc"])
    if not pool.is_finite() or pool <= 0 or not raw["source"].startswith("https://app.ondoperps.xyz/announcements/"):
        raise ValueError("리워드 정책 금액/공식 출처 오류")
    return RewardPolicy(raw["source"], dates[0].timestamp(), dates[1].timestamp(), frozenset(bases), pool)


def policy_for_config(cfg) -> RewardPolicy | None:
    if not cfg.rewards.policy_path:
        return None
    path = Path(cfg.rewards.policy_path)
    if not path.is_absolute():
        path = (cfg.path.parent if cfg.path else Path.cwd()) / path
    return load_policy(path)
