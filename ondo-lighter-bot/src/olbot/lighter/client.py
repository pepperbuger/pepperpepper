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


TERMINAL_FILLED = "filled"
OPEN_STATUSES = {"in-progress", "pending", "open"}


@dataclass(frozen=True)
class OrderStatus:
    """accountOrders 응답의 주문 한 건. 최종 상태면 filled가 그 주문의 확정 체결 수량이다."""
    client_order_index: int
    status: str
    filled: Decimal
    initial: Decimal

    @property
    def terminal(self) -> bool:
        # filled 또는 canceled-* (canceled-too-much-slippage, canceled-not-enough-liquidity 등)
        return self.status == TERMINAL_FILLED or self.status.startswith("canceled")


def parse_order_status(order: dict) -> OrderStatus:
    return OrderStatus(
        client_order_index=int(order["client_order_index"]),
        status=str(order.get("status") or ""),
        filled=Decimal(str(order.get("filled_base_amount") or "0")),
        initial=Decimal(str(order.get("initial_base_amount") or "0")),
    )


AUTH_TOKEN_TTL_SEC = 8 * 60  # SDK 기본 토큰은 10분짜리. 여유를 두고 8분마다 새로 만든다


class LighterTrader:
    def __init__(self, base_url: str, account_index: int, api_key_index: int, private_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.account_index = account_index
        self.api_key_index = api_key_index
        self._private_key = private_key
        self.client = None
        self._last_coi = 0
        self._auth_token: str | None = None
        self._auth_token_ts = 0.0

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

    def new_client_order_index(self) -> int:
        """주문 전에 미리 정해서 미확정 기록에 함께 저장한다. 그래야 응답을 잃어도 이 번호로 결과를 찾는다."""
        coi = int(time.time() * 1000) % (2**48 - 1)
        if coi <= self._last_coi:
            coi = self._last_coi + 1
        self._last_coi = coi
        return coi

    async def market_order(self, m: LighterMarket, size: Decimal, is_sell: bool, worst_price: Decimal,
                           client_order_index: int | None = None) -> str | None:
        """IOC 시장가 주문.

        - None: 거래소가 접수함 (체결 여부는 포지션으로 확인)
        - 문자열: 확실히 체결 안 됨 (서명 실패, HTTP 400 거절, 응답 코드가 200이 아님)
        - 예외: 결과를 모름 (네트워크 오류 등). 호출 쪽이 미확정 주문으로 다뤄야 한다.
        """
        base_amount = m.to_int_size(size)
        if base_amount <= 0:
            return "size rounds to zero"
        # api_key_index를 넘기지 않는다: 넘기면 SDK가 논스 관리자를 건너뛰는 경로를 탄다.
        # SignerClient에 키를 하나만 등록했으므로 SDK가 그 키와 다음 논스를 고른다.
        _tx, resp, err = await self.client.create_market_order(
            market_index=m.market_id,
            client_order_index=client_order_index if client_order_index is not None else self.new_client_order_index(),
            base_amount=base_amount,
            avg_execution_price=m.to_int_price(worst_price),
            is_ask=is_sell,
        )
        if err:
            return str(err)
        if resp is not None and getattr(resp, "code", 200) != 200:
            return f"code {resp.code}: {getattr(resp, 'message', '')}"
        log.info("lighter order accepted: %s %s %s worst=%s resp=%s", m.base, "sell" if is_sell else "buy", size, worst_price, resp)
        return None

    def _auth(self) -> str:
        now = time.time()
        if self._auth_token is None or now - self._auth_token_ts > AUTH_TOKEN_TTL_SEC:
            token, err = self.client.create_auth_token_with_expiry(api_key_index=self.api_key_index)
            if err:
                raise RuntimeError(f"Lighter 인증 토큰 생성 실패: {err}")
            self._auth_token, self._auth_token_ts = token, now
        return self._auth_token

    async def order_status(self, client_order_index: int) -> OrderStatus | None:
        """client_order_index로 주문 최종 상태를 조회한다. 거래소에 없으면 None.

        None은 "아직 반영 전"일 수도 있어서 미체결의 증거가 아니다. 조회 실패는 예외로 올린다.
        """
        url = (f"{self.base_url}/api/v1/accountOrders?account_index={self.account_index}"
               f"&client_order_indexes={client_order_index}")
        data = await get_json(url, headers={"authorization": self._auth()})
        if int(data.get("code", 200)) != 200:
            raise RuntimeError(f"accountOrders code {data.get('code')}: {data.get('message')}")
        for order in data.get("orders") or []:
            if int(order.get("client_order_index", -1)) == client_order_index:
                return parse_order_status(order)
        return None
