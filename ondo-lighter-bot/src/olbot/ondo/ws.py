"""Ondo Perps 공개 WebSocket: `topOfBooksPerps` 구독.

연결 직후 구독하면 마켓별 현재 최우선 호가가 오고, 이후에는 바뀔 때마다 온다.
180초 동안 아무 프레임이 없으면 서버가 끊으므로 `{"op":"ping"}`을 주기적으로 보낸다.
"""

from __future__ import annotations

from olbot.market import MarketBoard, ondo_market_name, parse_ondo_top_of_book
from olbot.wsbase import EventCallback, ReconnectingWS


class OndoPublicWS(ReconnectingWS):
    name = "ondo_ws"

    def __init__(self, url: str, board: MarketBoard, ping_sec: float, on_event: EventCallback | None = None):
        super().__init__(url, ping_sec, {"op": "ping"}, on_event)
        self.board = board

    def subscriptions(self) -> list[dict]:
        return [{
            "op": "subscribe",
            "channel": "topOfBooksPerps",
            "markets": [ondo_market_name(b) for b in self.board.bases],
        }]

    def on_message(self, msg: dict, recv_ts: float) -> None:
        if msg.get("type") == "error":
            self.log.warning("server error: %s", msg)
            return
        for base, bid, ask in parse_ondo_top_of_book(msg):
            self.board.update_ondo(base, bid, ask, recv_ts)
