"""공개 GET 전용 주간 관측. 키, 서명, 주문 클라이언트를 사용하지 않는다.

python -m olbot.reward_probe --duration 604800 --output outputs/reward-week.jsonl
공식 배분 공식 전체가 공개된 것은 아니므로 아래 수치는 정적인 비례배분 가정이다.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

from olbot.markets import _get_json
from olbot.rewards import load_policy

ONDO = "https://api.ondoperps.xyz"
LIGHTER = "https://mainnet.zklighter.elliot.ai"


def depth_summary(response: dict) -> dict:
    if response.get("success") is not True:
        raise ValueError("호가 조회 실패")
    book = response["result"]
    sides = [book["bids"], book["asks"]]
    levels = [[(D(p), D(s)) for p, s in side] for side in sides]
    if any(not side for side in levels) or any(not p.is_finite() or not s.is_finite() or p <= 0 or s < 0
                                              for side in levels for p, s in side):
        raise ValueError("호가 가격/수량 오류")
    if levels[0][0][0] >= levels[1][0][0]:
        raise ValueError("호가 역전")
    mid = (levels[0][0][0] + levels[1][0][0]) / 2
    outer = D(0)
    complete = True
    for index, side in enumerate(levels):
        prices = [p for p, _ in side]
        if prices != sorted(prices, reverse=index == 0):
            raise ValueError("호가 정렬 오류")
        for price, size in side:
            distance = abs(price / mid - 1) * 10000
            if D(11) <= distance <= D(25):
                outer += price * size
        if len(side) >= 100 and abs(side[-1][0] / mid - 1) * 10000 < 25:
            complete = False
    return {"mid": mid, "outer_11_25_usd": outer, "depth_complete": complete}


def share_scenario(competitor_usd: D, own_usd: D, pool_usdc: D) -> D:
    if any(not x.is_finite() or x < 0 for x in (competitor_usd, own_usd, pool_usdc)) or own_usd == 0:
        raise ValueError("시나리오 금액 오류")
    return pool_usdc * own_usd / (competitor_usd + own_usd)


def observe(policy, bases: list[str]) -> dict:
    # 정책이 만료돼도 공개 관측만 계속한다. 과거 정책으로 보상액을 계산하지 않는다.
    now = time.time()
    valid = policy.block_reason(bases, now) is None
    books = _get_json(LIGHTER + "/api/v1/orderBooks")["order_books"]
    lighter = {b["symbol"].upper(): b for b in books
               if b.get("status") == "active" and b.get("market_type") == "perp"}
    rows = [{"base": base, "lighter_hedge_listed": base in lighter} for base in bases]
    markets = _get_json(ONDO + "/v1/markets")["result"]["perps"]["tradingPairs"]
    mapping = {m["pair"]["base"].upper(): m["market"] for m in markets if not m.get("disabled")}
    for row in rows:
        base = row["base"]
        if base not in mapping:
            row["error"] = "Ondo 활성 마켓 없음"
            continue
        time.sleep(1.1)  # 공개 Ondo도 1초 요청 예산 준수. live와 동시에 실행하지 않는다.
        row.update(depth_summary(_get_json(ONDO + "/v1/perps/depth?market=" + mapping[base] + "&depth=100")))
        if valid:
            row["outer_pool_week_usdc"] = policy.per_market_usdc * D("0.05")
            row["both_60_share_scenario_week_usdc"] = share_scenario(
                row["outer_11_25_usd"], D(120), row["outer_pool_week_usdc"])
    return {"ts": now, "utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "policy_valid": valid, "policy_source": policy.source, "rows": rows,
            "orders_sent": 0, "note": "정적 비례배분 가정; 실제 보상 예측·체결 헷지 시험 아님"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default="docs/rewards-2026-10-27.json")
    parser.add_argument("--output", required=True)
    parser.add_argument("--duration", type=float, default=604800)
    parser.add_argument("--interval", type=float, default=300)
    args = parser.parse_args(argv)
    if args.duration <= 0 or args.interval < 60:
        parser.error("기간 > 0, 간격 >= 60초 필요")
    policy = load_policy(args.policy)
    # 주식 주말/휴장 검증은 이 관측 범위에 포함하지 않는다.
    bases = [b for b in ("BTC", "ETH") if b in policy.allowed_bases]
    if not bases:
        parser.error("확인된 24시간 암호화폐 마켓 없음")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    status = output.with_suffix(".status.json")
    deadline = time.monotonic() + args.duration
    started = time.time()
    samples = errors = 0
    try:
        with output.open("a", encoding="utf-8") as stream:
            while time.monotonic() < deadline:
                try:
                    record = observe(policy, bases)
                    samples += 1
                except Exception as exc:
                    errors += 1
                    record = {"utc": datetime.now(timezone.utc).isoformat(), "error_type": type(exc).__name__,
                              "orders_sent": 0}
                stream.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
                stream.flush()
                status.write_text(json.dumps({"started_ts": started, "updated_ts": time.time(),
                                             "samples": samples, "errors": errors, "state": "observing",
                                             "orders_sent": 0}, ensure_ascii=False), encoding="utf-8")
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(args.interval, remaining))
    finally:
        status.write_text(json.dumps({"started_ts": started, "updated_ts": time.time(), "samples": samples,
                                     "errors": errors, "state": "stopped", "orders_sent": 0}), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
