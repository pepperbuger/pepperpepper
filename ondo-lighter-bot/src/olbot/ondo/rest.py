"""Ondo Perps 인증 REST 클라이언트 (API 키 HMAC 서명).

서명: hex(HMAC-SHA256(secret, timestamp_ms + METHOD + path_with_query + body)).
서명에 쓴 body 문자열을 그대로 전송해야 하므로 json.dumps 결과를 한 번만 만든다.
모든 호출은 하나의 TokenBucket을 거친다 (공개 베타 기본 초당 1회).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from olbot.ratelimit import TokenBucket


class OndoError(Exception):
    def __init__(self, status: int, error_code: str, message: str) -> None:
        super().__init__(f"HTTP {status} {error_code}: {message}")
        self.status = status
        self.error_code = error_code


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
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except ValueError:
                return e.code, {"success": False, "error_code": "non_json", "error": raw[:200].decode(errors="replace")}

    async def request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None):
        if params:
            path = path + "?" + urllib.parse.urlencode(params, safe=",:")
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        await self.bucket.acquire()
        status, data = await asyncio.to_thread(self._do, method, path, body)
        if status >= 400 or data.get("success") is False:
            raise OndoError(status, str(data.get("error_code", "")), str(data.get("error", ""))[:300])
        return data.get("result")

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
