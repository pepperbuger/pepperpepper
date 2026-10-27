"""두 거래소의 최우선 호가를 마켓별로 모으고 괴리(bps)를 계산한다.

괴리 = (Lighter 중간가 / Ondo 중간가 - 1) x 10,000.
양수면 Lighter가 비싸다. 두 거래소 사이에는 평소 괴리가 꾸준히 있으므로,
호가 로직은 괴리가 최근 중앙값에서 basis_deviation_bps 이상 벗어난 마켓에 호가를 걸지 않는다.
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class Quote:
    bid: Decimal
    ask: Decimal
    recv_ts: float  # 이 프로세스가 받은 시각 (time.time())

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> float:
        return float((self.ask - self.bid) / self.mid * 10_000)

    def age(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.recv_ts


@dataclass
class MarketState:
    base: str
    ondo: Quote | None = None
    lighter: Quote | None = None

    def basis_bps(self) -> float | None:
        if self.ondo is None or self.lighter is None:
            return None
        return float((self.lighter.mid / self.ondo.mid - 1) * 10_000)


class BasisTracker:
    """최근 window_sec 동안의 괴리 샘플을 들고 중앙값을 낸다."""

    def __init__(self, window_sec: float) -> None:
        self.window_sec = window_sec
        self.samples: deque[tuple[float, float]] = deque()

    def add(self, ts: float, bps: float) -> None:
        self.samples.append((ts, bps))
        self._prune(ts)

    def _prune(self, now: float) -> None:
        while self.samples and self.samples[0][0] < now - self.window_sec:
            self.samples.popleft()

    def median(self, now: float) -> float | None:
        self._prune(now)
        if not self.samples:
            return None
        return statistics.median(b for _, b in self.samples)


def basis_status(bps: float | None, median: float | None, deviation_bps: float, sanity_bps: float) -> str:
    """ok | missing | sanity(다른 자산으로 의심) | deviation(평소 괴리에서 이탈)."""
    if bps is None:
        return "missing"
    if abs(bps) > sanity_bps:
        return "sanity"
    if median is not None and abs(bps - median) > deviation_bps:
        return "deviation"
    return "ok"


def ondo_market_name(base: str) -> str:
    return f"{base.upper()}-USD.P"


def base_from_ondo_market(market: str) -> str:
    return market.split("-", 1)[0].upper()


@dataclass
class MarketBoard:
    bases: list[str]
    states: dict[str, MarketState] = field(init=False)

    def __post_init__(self) -> None:
        self.states = {b: MarketState(b) for b in self.bases}

    def update_ondo(self, base: str, bid: Decimal, ask: Decimal, recv_ts: float) -> None:
        st = self.states.get(base)
        if st is not None and bid > 0 and ask > bid:
            st.ondo = Quote(bid, ask, recv_ts)

    def update_lighter(self, base: str, bid: Decimal, ask: Decimal, recv_ts: float) -> None:
        st = self.states.get(base)
        if st is not None and bid > 0 and ask > bid:
            st.lighter = Quote(bid, ask, recv_ts)


def parse_ondo_top_of_book(msg: dict) -> list[tuple[str, Decimal, Decimal]]:
    """Ondo `topOfBooksPerps` update -> [(base, bid, ask)]. 한쪽 호가가 비어 있으면 건너뛴다."""
    if msg.get("type") != "update" or msg.get("channel") != "topOfBooksPerps":
        return []
    out = []
    for book in msg.get("data") or []:
        bids, asks = book.get("bids") or [], book.get("asks") or []
        if not bids or not asks:
            continue
        out.append((base_from_ondo_market(book["market"]), Decimal(bids[0][0]), Decimal(asks[0][0])))
    return out


def parse_lighter_ticker(msg: dict) -> tuple[int, Decimal, Decimal] | None:
    """Lighter `ticker/{market_id}` 메시지 -> (market_id, bid, ask)."""
    if msg.get("type") not in ("subscribed/ticker", "update/ticker"):
        return None
    channel = msg.get("channel", "")
    ticker = msg.get("ticker") or {}
    try:
        market_id = int(channel.split(":", 1)[1])
        bid = Decimal(ticker["b"]["price"])
        ask = Decimal(ticker["a"]["price"])
    except (IndexError, KeyError, TypeError, ValueError, ArithmeticError):
        return None
    return market_id, bid, ask
