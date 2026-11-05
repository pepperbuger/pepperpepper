"""Ondo Perps 인증 REST 클라이언트 (API 키 HMAC 서명).

서명: hex(HMAC-SHA256(secret, timestamp_ms + METHOD + path_with_query + body)).
서명에 쓴 body 문자열을 그대로 전송해야 하므로 json.dumps 결과를 한 번만 만든다.
모든 호출은 하나의 TokenBucket을 거친다 (공개 베타 기본 초당 1회).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from olbot.ratelimit import TokenBucket
from olbot.ondo.transport import rest_opener


class OndoError(Exception):
    def __init__(self, status: int, error_code: str, message: str) -> None:
        super().__init__(f"HTTP {status} {error_code}: {message}")
        self.status = status
        self.error_code = error_code


@dataclass(frozen=True)
class OpenOrder:
    order_id: str
    market: str
    side: str


def parse_open_orders(data: dict, limit: int = 1000) -> dict[str, OpenOrder]:
    """완전한 열린 주문 스냅샷만 허용한다. 잘린 목록은 누락 판정에 쓰지 않는다."""
    if not isinstance(data, dict) or data.get("success") is not True:
        raise ValueError("열린 주문 조회 성공을 확인하지 못함")
    rows = data["result"]
    page = data.get("pageInfo")
    if (not isinstance(rows, list) or len(rows) >= limit or
            (page is not None and (not isinstance(page, dict) or page.get("nextCursor")))):
        raise ValueError("열린 주문 목록이 불완전하여 대사를 보류한다")
    if page is not None and page.get("nextCursor") is not None and not isinstance(page["nextCursor"], str):
        raise ValueError("열린 주문 페이지 식별값 오류")
    result = {}
    for row in rows:
        oid, market, side = row["orderId"], row["market"], row["side"]
        if (not isinstance(oid, str) or not oid.strip() or oid in result or
                not isinstance(market, str) or not market.strip() or
                side not in {"buy", "sell"} or row["status"] != "open"):
            raise ValueError("열린 주문 응답 식별값/상태 오류")
        result[oid] = OpenOrder(oid, market, side)
    return result


def sign(secret: str, timestamp_ms: str, method: str, path_with_query: str, body: str) -> str:
    msg = timestamp_ms + method.upper() + path_with_query + body
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def ws_login_message(key_id: str, secret: str, now_ms: int | None = None) -> dict:
    t = str(now_ms if now_ms is not None else int(time.time() * 1000))
    sig = hmac.new(secret.encode(), (t + "ondo_perps_ws_login").encode(), hashlib.sha256).hexdigest()
    return {"op": "login", "args": {"key": key_id, "time": t, "sign": sig}}


def signed_position(p: dict) -> Decimal:
    """positions 응답 한 줄 -> 부호 있는 수량 (롱 +, 숏 -)."""
    qty = abs(Decimal(p.get("netQuantity") or "0"))
    direction = p.get("direction")
    if direction == "short":
        return -qty
    if direction == "long":
        return qty
    return Decimal(0)


class OndoRest:
    def __init__(self, base_url: str, key_id: str, secret: str, bucket: TokenBucket, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.key_id = key_id
        self.secret = secret
        self.bucket = bucket
        self.timeout = timeout
        self._opener = rest_opener()

    def _do(self, method: str, path: str, body: str) -> tuple[int, dict]:
        ts = str(int(time.time() * 1000))
        headers = {
            "ONDO-KEY-ID": self.key_id,
            "ONDO-TIMESTAMP": ts,
            "ONDO-SIGN": sign(self.secret, ts, method, path, body),
            "Content-Type": "application/json",
            "User-Agent": "olbot/0.1",
        }
        req = urllib.request.Request(
            self.base_url + path, data=body.encode() if body else None, method=method, headers=headers
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except ValueError:
                return e.code, {"success": False, "error_code": "non_json", "error": raw[:200].decode(errors="replace")}

    async def request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None):
        data = await self._request_response(method, path, params, payload)
        return data.get("result")

    async def _request_response(self, method: str, path: str, params: dict | None = None, payload: dict | None = None):
        if params:
            path = path + "?" + urllib.parse.urlencode(params, safe=",:")
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        await self.bucket.acquire()
        status, data = await asyncio.to_thread(self._do, method, path, body)
        if status >= 400 or data.get("success") is False:
            raise OndoError(status, str(data.get("error_code", "")), str(data.get("error", ""))[:300])
        return data

    async def open_orders(self) -> dict[str, OpenOrder]:
        """GET /v1/perps/orders?status=open&limit=1000 (공식 Get Orders 명세)."""
        response = await self._request_response("GET", "/v1/perps/orders", {"status": "open", "limit": 1000})
        return parse_open_orders(response)

    async def balance(self) -> dict:
        return await self.request("GET", "/v1/perps/balance")

    async def positions(self) -> dict[str, Decimal]:
        """market -> 부호 있는 수량. 응답에 없는 마켓은 0이다."""
        rows = await self.request("GET", "/v1/perps/positions") or []
        return {p["market"]: signed_position(p) for p in rows}

    async def set_leverage(self, market: str, leverage: int) -> None:
        await self.request("POST", "/v1/perps/leverage", payload={"market": market, "leverage": str(leverage)})

    async def cancel_all(self, market: str | None = None) -> None:
        await self.request("DELETE", "/v1/perps/orders", params={"market": market} if market else None)

    async def create_batch(self, orders: list[dict]) -> tuple[list[dict], list[dict]]:
        res = await self.request("POST", "/v1/perps/orders/batch", payload={"orders": orders}) or {}
        return res.get("addedOrders") or [], res.get("failedOrders") or []
