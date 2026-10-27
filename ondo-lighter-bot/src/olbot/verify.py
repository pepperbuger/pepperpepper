"""실제 키 확인 (주문 없음, 데드맨 스위치도 켜지 않음).

샌드박스 없이 가므로 실운영 전에 이것부터 돌린다.
- Ondo REST: 서명이 맞는지 (잔고, 포지션 조회)
- Ondo WS: API 키 로그인
- Lighter: API 키가 계정에 등록됐는지(check_client), 계정 조회, 주문 조회용 인증 토큰
"""

from __future__ import annotations

import asyncio
import json

import websockets

from olbot.config import Config, Secrets
from olbot.ondo.rest import OndoRest, ws_login_message
from olbot.ratelimit import TokenBucket


async def _ondo_ws_login(url: str, key_id: str, secret: str, timeout: float = 10.0) -> str:
    async with websockets.connect(url, ping_interval=None, close_timeout=3) as ws:
        await ws.send(json.dumps(ws_login_message(key_id, secret)))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if msg.get("type") == "loggedIn":
                return "ok"
            if msg.get("type") == "error":
                return f"error: {msg}"


async def verify_keys(cfg: Config, s: Secrets) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))

    rest = OndoRest(cfg.ondo.rest_url, s.ondo_api_key_id, s.ondo_api_secret, TokenBucket(1.0, 2))
    try:
        bal = await rest.balance()
        add("Ondo REST 잔고", True, f"marginBalance {bal.get('marginBalance')} USDC, 사용 증거금 {bal.get('usedMargin')}")
    except Exception as e:
        add("Ondo REST 잔고", False, f"{e} (키 권한, IP 화이트리스트, PC 시계 확인)")
    try:
        pos = await rest.positions()
        add("Ondo REST 포지션", True, f"{len(pos)}개 열림: {pos}" if pos else "열린 포지션 없음")
    except Exception as e:
        add("Ondo REST 포지션", False, str(e))
    try:
        r = await _ondo_ws_login(cfg.ondo.ws_url, s.ondo_api_key_id, s.ondo_api_secret)
        add("Ondo WS 로그인", r == "ok", r)
    except Exception as e:
        add("Ondo WS 로그인", False, repr(e))

    from olbot.lighter.client import LighterTrader

    t = LighterTrader(cfg.lighter.base_url, int(s.lighter_account_index), int(s.lighter_api_key_index),
                      s.lighter_api_key_private_key)
    try:
        await t.connect()
        add("Lighter API 키 등록", True, f"계정 {s.lighter_account_index}, 키 인덱스 {s.lighter_api_key_index}")
        acct = await t.account()
        add("Lighter 계정 조회", True, f"자산 {acct.get('total_asset_value')} USDC, 사용 가능 {acct.get('available_balance')}")
        # 존재할 리 없는 주문 번호로 조회: 인증이 맞으면 "없음"이 정상 응답이다
        st = await t.order_status(1)
        add("Lighter 주문 조회 인증", True, "정상 (테스트 주문 번호 1: 없음)" if st is None else f"응답 {st}")
    except Exception as e:
        add("Lighter", False, f"{e!r} (계정 번호, 키 인덱스, 개인키, 네트워크(메인넷/테스트넷) 확인)")
    finally:
        try:
            await t.close()
        except Exception:
            pass
    return results
