"""Lighter 공개 WebSocket: 마켓별 `ticker/{market_id}`(최우선 매수/매도 호가) 구독.

2분 동안 아무 프레임이 없으면 서버가 끊으므로 `{"type":"ping"}`을 주기적으로 보낸다.
"""

from __future__ import annotations

from olbot.market import MarketBoard, parse_lighter_ticker
from olbot.wsbase import EventCallback, ReconnectingWS


class LighterPublicWS(ReconnectingWS):
    name = "lighter_ws"

    def __init__(
        self,
        url: str,
        board: MarketBoard,
        market_ids: dict[str, int],
        ping_sec: float,
        on_event: EventCallback | None = None,
    ):
        super().__init__(url, ping_sec, {"type": "ping"}, on_event)
        self.board = board
        self.market_ids = market_ids
        self.base_by_id = {mid: base for base, mid in market_ids.items()}

    def subscriptions(self) -> list[dict]:
        return [{"type": "subscribe", "channel": f"ticker/{mid}"} for mid in self.market_ids.values()]

    def on_message(self, msg: dict, recv_ts: float) -> None:
        if msg.get("type") == "error" or "error" in msg:
            self.log.warning("server error: %s", msg)
            return
        parsed = parse_lighter_ticker(msg)
        if parsed is None:
            return
        market_id, bid, ask = parsed
        base = self.base_by_id.get(market_id)
        if base is not None:
            self.board.update_lighter(base, bid, ask, recv_ts)
