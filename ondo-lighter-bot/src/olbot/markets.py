"""설정의 기초자산(base)을 각 거래소의 마켓으로 연결한다. 공개 REST만 쓰므로 키가 필요 없다."""

from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import dataclass

from olbot.market import ondo_market_name


def _get_json(url: str, timeout: float = 20.0, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "olbot/0.1", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


async def get_json(url: str, timeout: float = 20.0, headers: dict | None = None) -> dict:
    return await asyncio.to_thread(_get_json, url, timeout, headers)


async def fetch_ondo_perps(rest_url: str) -> dict[str, dict]:
    """base -> Ondo 퍼프 마켓 정보 (비활성 마켓 제외)."""
    data = await get_json(f"{rest_url}/v1/markets")
    pairs = data["result"]["perps"]["tradingPairs"]
    return {p["pair"]["base"].upper(): p for p in pairs if not p.get("disabled")}


async def fetch_lighter_perps(rest_url: str) -> dict[str, dict]:
    """base(symbol) -> Lighter 퍼프 마켓 정보 (활성 마켓만)."""
    data = await get_json(f"{rest_url}/api/v1/orderBooks")
    return {
        b["symbol"].upper(): b
        for b in data["order_books"]
        if b.get("status") == "active" and b.get("market_type") == "perp"
    }


@dataclass
class Resolved:
    lighter_ids: dict[str, int]
    ondo_markets: dict[str, str]
    problems: list[str]


async def resolve(bases: list[str], ondo_rest_url: str, lighter_rest_url: str) -> Resolved:
    ondo, lighter = await asyncio.gather(fetch_ondo_perps(ondo_rest_url), fetch_lighter_perps(lighter_rest_url))
    problems = []
    lighter_ids: dict[str, int] = {}
    ondo_markets: dict[str, str] = {}
    for base in bases:
        if base not in ondo:
            problems.append(f"{base}: Ondo에 활성 퍼프 마켓 없음")
        else:
            ondo_markets[base] = ondo[base]["market"]
            if ondo[base]["market"] != ondo_market_name(base):
                problems.append(f"{base}: Ondo 마켓 이름이 예상과 다름 ({ondo[base]['market']})")
        if base not in lighter:
            problems.append(f"{base}: Lighter에 활성 퍼프 마켓 없음 (헷지 불가)")
        else:
            lighter_ids[base] = int(lighter[base]["market_id"])
    return Resolved(lighter_ids, ondo_markets, problems)
