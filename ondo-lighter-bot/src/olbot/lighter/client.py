"""Lighter 헷지 클라이언트. 주문 서명은 공식 SDK(lighter-sdk)의 SignerClient를 쓴다.

가격과 수량은 정수(값 x 10^decimals)로 보낸다. decimals와 최소 주문은 orderBookDetails에서 받는다.
헷지는 IOC 시장가 주문이고, price 자리에는 감수할 최악 가격을 넣는다.
`200` 응답은 접수일 뿐이므로 체결 여부는 포지션을 다시 읽어서 확인한다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from olbot.markets import get_json

log = logging.getLogger("olbot.lighter")


@dataclass(frozen=True)
class LighterMarket:
    base: str
    market_id: int
    size_decimals: int
    price_decimals: int
    min_base: Decimal
    min_quote: Decimal

    def to_int_size(self, size: Decimal) -> int:
        return int((size * (Decimal(10) ** self.size_decimals)).to_integral_value(rounding=ROUND_DOWN))

    def to_int_price(self, price: Decimal) -> int:
        return int((price * (Decimal(10) ** self.price_decimals)).to_integral_value(rounding=ROUND_DOWN))

    def min_hedge_size(self, price: Decimal) -> Decimal:
        """이보다 작은 차이는 한 번에 헷지할 수 없어서 쌓아뒀다가 넘으면 보낸다."""
        return max(self.min_base, self.min_quote * Decimal("1.05") / price)


async def fetch_market_meta(rest_url: str, base: str, market_id: int) -> LighterMarket:
    data = await get_json(f"{rest_url}/api/v1/orderBookDetails?market_id={market_id}")
    d = data["order_book_details"][0]
    return LighterMarket(
        base=base,
        market_id=market_id,
        size_decimals=int(d["supported_size_decimals"]),
        price_decimals=int(d["supported_price_decimals"]),
        min_base=Decimal(str(d["min_base_amount"])),
        min_quote=Decimal(str(d["min_quote_amount"])),
    )


def parse_account_positions(account: dict) -> dict[int, Decimal]:
    """account 응답 -> market_id별 부호 있는 수량."""
    out: dict[int, Decimal] = {}
    for p in account.get("positions") or []:
        qty = Decimal(str(p.get("position") or "0"))
        out[int(p["market_id"])] = qty * int(p.get("sign") or 0)
    return out


class LighterTrader:
    def __init__(self, base_url: str, account_index: int, api_key_index: int, private_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.account_index = account_index
        self.api_key_index = api_key_index
        self._private_key = private_key
        self.client = None
        self._last_coi = 0

    async def connect(self) -> None:
        import lighter  # 무거운 의존성이라 실제 거래할 때만 불러온다

        self.client = lighter.SignerClient(
            url=self.base_url,
            account_index=self.account_index,
            api_private_keys={self.api_key_index: self._private_key},
        )
        err = self.client.check_client()
        if err is not None:
            raise RuntimeError(f"Lighter API 키 확인 실패: {err}")

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()

    async def account(self) -> dict:
        data = await get_json(f"{self.base_url}/api/v1/account?by=index&value={self.account_index}")
        return data["accounts"][0]

    async def positions(self) -> dict[int, Decimal]:
        return parse_account_positions(await self.account())

    def _next_client_order_index(self) -> int:
        coi = int(time.time() * 1000) % (2**48 - 1)
        if coi <= self._last_coi:
            coi = self._last_coi + 1
        self._last_coi = coi
        return coi

    async def market_order(self, m: LighterMarket, size: Decimal, is_sell: bool, worst_price: Decimal) -> str | None:
        """IOC 시장가 주문. 접수되면 None, 실패하면 오류 문자열."""
        base_amount = m.to_int_size(size)
        if base_amount <= 0:
            return "size rounds to zero"
        _tx, resp, err = await self.client.create_market_order(
            market_index=m.market_id,
            client_order_index=self._next_client_order_index(),
            base_amount=base_amount,
            avg_execution_price=m.to_int_price(worst_price),
            is_ask=is_sell,
            api_key_index=self.api_key_index,
        )
        if err:
            return str(err)
        log.info("lighter order accepted: %s %s %s worst=%s resp=%s", m.base, "sell" if is_sell else "buy", size, worst_price, resp)
        return None
