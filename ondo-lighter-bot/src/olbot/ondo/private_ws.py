"""Ondo Perps 비공개 WebSocket: 로그인, 체결(fillsPerps), 포지션(positionsPerps), 데드맨 스위치.

데드맨 스위치(cancelAllOrdersAfterPerps)는 이 연결로 들어오는 클라이언트 메시지(ping 포함)가
timeout_seconds 동안 없으면 계정의 모든 주문을 취소한다. 연결이 끊겨도 타이머는 살아 있다.
그래서 ping 간격(deadman_heartbeat_sec)을 timeout보다 충분히 짧게 둔다.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Callable

from olbot.ondo.rest import signed_position, ws_login_message
from olbot.wsbase import EventCallback, ReconnectingWS

FillCallback = Callable[[dict], None]


class OndoPrivateWS(ReconnectingWS):
    name = "ondo_private_ws"

    def __init__(
        self,
        url: str,
        key_id: str,
        secret: str,
        markets: list[str],
        deadman_timeout_sec: int,
        heartbeat_sec: float,
        on_fill: FillCallback,
        on_event: EventCallback | None = None,
    ) -> None:
        super().__init__(url, heartbeat_sec, {"op": "ping"}, on_event)
        self.key_id = key_id
        self.secret = secret
        self.markets = markets
        self.deadman_timeout_sec = deadman_timeout_sec
        self.on_fill = on_fill
        self.logged_in = False
        self.deadman_armed = False
        self.positions: dict[str, Decimal] = {}
        self.positions_ts = 0.0

    @property
    def ready(self) -> bool:
        return self.connected and self.logged_in and self.deadman_armed

    def private_subscriptions(self) -> list[dict]:
        return [
            {"op": "subscribe", "channel": "fillsPerps", "markets": self.markets},
            {"op": "subscribe", "channel": "positionsPerps"},
            {"op": "subscribe", "channel": "cancelAllOrdersAfterPerps", "timeout_seconds": self.deadman_timeout_sec},
        ]

    def subscriptions(self) -> list[dict]:
        # 비공개 채널은 로그인 확인(loggedIn)을 받은 뒤에 구독한다
        self.logged_in = False
        self.deadman_armed = False
        return [ws_login_message(self.key_id, self.secret)]

    def on_message(self, msg: dict, recv_ts: float) -> None:
        typ = msg.get("type")
        if typ == "loggedIn":
            self.logged_in = True
            self.log.info("logged in")
            for sub in self.private_subscriptions():
                self.send_nowait(sub)
            return
        if typ == "subscribed":
            if msg.get("channel") == "cancelAllOrdersAfterPerps":
                self.deadman_armed = True
                self.log.info("dead man's switch armed (%ss)", self.deadman_timeout_sec)
            return
        if typ == "error":
            self.log.error("server error: %s", msg)
            return
        if typ != "update":
            return
        channel = msg.get("channel")
        data = msg.get("data") or []
        if channel == "fillsPerps":
            for fill in data:
                self.on_fill(fill)
        elif channel == "positionsPerps":
            for p in data:
                self.positions[p["market"]] = signed_position(p)
            self.positions_ts = recv_ts
