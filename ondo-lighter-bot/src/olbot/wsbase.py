"""끊기면 다시 붙는 WebSocket 클라이언트 공통 부분.

하위 클래스는 subscriptions()와 on_message()만 구현한다.
연결/끊김은 on_event 콜백으로 알려서 ledger에 남긴다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import websockets

EventCallback = Callable[[str, str, str], Awaitable[None] | None]


class ReconnectingWS:
    name = "ws"

    def __init__(
        self,
        url: str,
        ping_sec: float,
        ping_payload: dict,
        on_event: EventCallback | None = None,
        max_backoff_sec: float = 30.0,
    ) -> None:
        self.url = url
        self.ping_sec = ping_sec
        self.ping_payload = ping_payload
        self.on_event = on_event
        self.max_backoff_sec = max_backoff_sec
        self.log = logging.getLogger(f"olbot.{self.name}")
        self.connected = False
        self.last_msg_ts = 0.0
        self.connect_count = 0
        self.disconnect_count = 0
        self._stop = asyncio.Event()
        self._ws = None

    def send_nowait(self, payload: dict) -> None:
        """on_message 안에서 응답을 보내야 할 때 쓴다 (예: 로그인 확인 후 구독)."""
        if self._ws is not None:
            asyncio.get_running_loop().create_task(self._ws.send(json.dumps(payload)))

    def subscriptions(self) -> list[dict]:
        raise NotImplementedError

    def on_message(self, msg: dict, recv_ts: float) -> None:
        raise NotImplementedError

    async def _emit(self, kind: str, detail: str = "") -> None:
        if self.on_event is None:
            return
        res = self.on_event(self.name, kind, detail)
        if asyncio.iscoroutine(res):
            await res

    def stop(self) -> None:
        self._stop.set()

    def stale_for(self, now: float | None = None) -> float:
        """마지막 메시지 이후 지난 초. 연결 전이면 무한대."""
        if not self.last_msg_ts:
            return float("inf")
        return (now if now is not None else time.time()) - self.last_msg_ts

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, ping_interval=None, max_size=2**22, close_timeout=3) as ws:
                    self._ws = ws
                    self.connected = True
                    self.connect_count += 1
                    await self._emit("connected", self.url)
                    self.log.info("connected %s", self.url)
                    for sub in self.subscriptions():
                        await ws.send(json.dumps(sub))
                    backoff = 1.0
                    pinger = asyncio.create_task(self._ping_loop(ws))
                    stopper = asyncio.create_task(self._stop.wait())
                    try:
                        await self._read_loop(ws, stopper)
                    finally:
                        pinger.cancel()
                        stopper.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 네트워크 오류는 전부 재연결로 처리
                self.log.warning("connection error: %r", e)
                await self._emit("error", repr(e)[:300])
            self._ws = None
            if self.connected:
                self.connected = False
                if self._stop.is_set():
                    await self._emit("closed", "")
                else:
                    self.disconnect_count += 1
                    await self._emit("disconnected", "")
            if self._stop.is_set():
                break
            self.log.info("reconnecting in %.0fs", backoff)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self.max_backoff_sec)

    async def _read_loop(self, ws, stopper: asyncio.Task) -> None:
        while not stopper.done():
            recv = asyncio.create_task(ws.recv())
            done, _ = await asyncio.wait({recv, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if recv not in done:
                recv.cancel()
                return
            raw = recv.result()
            now = time.time()
            self.last_msg_ts = now
            try:
                msg = json.loads(raw)
            except ValueError:
                self.log.debug("non-json frame: %r", raw[:200])
                continue
            if isinstance(msg, dict):
                self.on_message(msg, now)

    async def _ping_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(self.ping_sec)
            await ws.send(json.dumps(self.ping_payload))
