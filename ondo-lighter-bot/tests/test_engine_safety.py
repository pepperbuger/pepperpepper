"""PR #1 검토에서 재현된 두 가지 결함의 재현 테스트.

1. 취소가 실패해도 로컬 주문 추적을 지워서, 거래소에 호가가 남아 있는데 다시 취소하지 않았다.
2. Lighter 주문 중 통신 예외가 나면 헷지 루프가 죽고 실패 횟수도 늘지 않았다.
"""

import asyncio
from decimal import Decimal as D
from pathlib import Path

from olbot.config import Secrets, load_config
from olbot.engine import Engine
from olbot.hedger import Hedger
from olbot.ledger import Ledger
from olbot.lighter.client import LighterMarket
from olbot.quoter import QuoteSide, TargetQuotes

ROOT = Path(__file__).resolve().parent.parent
LIT = LighterMarket("LIT", 120, size_decimals=2, price_decimals=4, min_base=D("1"), min_quote=D("10"))
QUOTES = TargetQuotes(QuoteSide(D("2.99"), D("20")), QuoteSide(D("3.01"), D("20")))


class FlakyOndo:
    """cancel_all이 처음 fail_times번은 실패하는 가짜 Ondo REST."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.cancel_calls: list[str | None] = []

    async def cancel_all(self, market=None):
        self.cancel_calls.append(market)
        if len(self.cancel_calls) <= self.fail_times:
            raise OSError("timeout")


def make_engine(ondo) -> Engine:
    cfg = load_config(ROOT / "config.live-500.example.yaml")
    eng = Engine(cfg, Secrets(), live=True, ledger=Ledger(":memory:"))
    eng.ondo_rest = ondo
    eng.ondo_meta = {b: {"market": f"{b}-USD.P"} for b in eng.bases}
    for b in eng.bases:
        eng.current[b] = QUOTES
    return eng


async def test_failed_market_cancel_keeps_tracking_and_is_retried():
    ondo = FlakyOndo(fail_times=1)
    eng = make_engine(ondo)
    assert await eng.pull_market("LIT", "deviation") is False
    assert eng.current["LIT"] is not None  # 거래소에 남아 있을 수 있으니 계속 추적
    assert await eng.pull_market("LIT", "deviation") is True
    assert eng.current["LIT"] is None
    assert ondo.cancel_calls == ["LIT-USD.P", "LIT-USD.P"]


async def test_failed_cancel_all_keeps_tracking_so_quote_loop_retries():
    ondo = FlakyOndo(fail_times=10)
    eng = make_engine(ondo)
    await eng.kill("test")
    assert eng.killed == "test"
    assert all(eng.current[b] is not None for b in eng.bases)


async def test_shutdown_cancel_retries_until_success(monkeypatch):
    monkeypatch.setattr("olbot.engine.asyncio.sleep", _no_sleep)
    ondo = FlakyOndo(fail_times=2)
    eng = make_engine(ondo)
    assert await eng.cancel_all_quotes(attempts=3) is True
    assert len(ondo.cancel_calls) == 3
    assert all(eng.current[b] is None for b in eng.bases)


async def test_quote_failure_marks_market_as_unknown_not_empty():
    class BrokenCreate(FlakyOndo):
        async def create_batch(self, orders):
            raise OSError("timeout after send")

    eng = make_engine(BrokenCreate(fail_times=0))
    eng.current["LIT"] = None
    await eng.place("LIT", QUOTES)
    # 요청이 거래소에 닿았는지 모르므로 "비어 있음"이 아니라 "확인 필요"로 남겨 다음에 취소를 시도한다
    assert eng.current["LIT"] is not None


class ExplodingTrader:
    def __init__(self) -> None:
        self.pos = {120: D("0")}
        self.calls = 0

    async def positions(self):
        return dict(self.pos)

    async def market_order(self, m, size, is_sell, worst):
        self.calls += 1
        raise ConnectionError("lighter down")


async def test_hedger_survives_order_exception_and_counts_failure(monkeypatch):
    monkeypatch.setattr("olbot.hedger.asyncio.sleep", _no_sleep)
    h = Hedger(ExplodingTrader(), {"LIT": LIT}, 15, 3)
    h.set_ondo_positions({"LIT": D("20")})
    await h.hedge("LIT", D("3"))  # 예외가 밖으로 나오면 안 된다
    assert h.states["LIT"].consecutive_failures == 1
    await h.hedge("LIT", D("3"))
    await h.hedge("LIT", D("3"))
    assert h.states["LIT"].consecutive_failures == 3  # 3이면 위험 판단이 kill한다


async def test_hedge_loop_keeps_running_when_lighter_raises(monkeypatch):
    """검토에서 재현된 경로: Lighter 주문 중 예외 -> 헷지 루프가 죽으면 안 되고 실패가 누적돼야 한다."""
    monkeypatch.setattr("olbot.hedger.asyncio.sleep", _no_sleep)
    eng = make_engine(FlakyOndo(fail_times=0))
    trader = ExplodingTrader()
    eng.lighter_markets = {b: LIT for b in eng.bases}
    eng.hedger = Hedger(trader, eng.lighter_markets, 15, 3)
    eng.hedger.set_ondo_positions({"LIT": D("20")})  # 헷지 안 된 롱 60달러
    now = 1_000_000.0
    eng.board.update_lighter("LIT", D("2.999"), D("3.001"), now)
    eng.board.update_ondo("LIT", D("2.999"), D("3.001"), now)
    rounds = {"n": 0}

    async def reconcile():
        rounds["n"] += 1
        if rounds["n"] >= 3:  # 3번째 회차까지 헷지하고 끝낸다
            eng.stop.set()

    eng.reconcile_positions = reconcile
    eng.cfg.hedging.reconcile_interval_sec = 0
    await asyncio.wait_for(eng.hedge_loop(), timeout=5)
    assert rounds["n"] == 3  # 예외에도 루프가 계속 돌았다
    assert eng.hedger.states["LIT"].consecutive_failures == 3
    assert trader.calls == 9  # 회차마다 3번씩 시도


async def test_hedge_after_kill_still_runs(monkeypatch):
    """kill은 호가만 멈춘다. 헷지 루프는 계속 노출을 줄여야 한다."""
    monkeypatch.setattr("olbot.hedger.asyncio.sleep", _no_sleep)
    eng = make_engine(FlakyOndo(fail_times=0))
    await eng.kill("test")

    class OkTrader(ExplodingTrader):
        async def market_order(self, m, size, is_sell, worst):
            self.calls += 1
            self.pos[m.market_id] += -size if is_sell else size

    eng.lighter_markets = {b: LIT for b in eng.bases}
    eng.hedger = Hedger(OkTrader(), eng.lighter_markets, 15, 3)
    eng.hedger.set_ondo_positions({"LIT": D("20")})
    eng.board.update_lighter("LIT", D("2.999"), D("3.001"), 1.0)

    async def reconcile():
        eng.stop.set()

    eng.reconcile_positions = reconcile
    eng.cfg.hedging.reconcile_interval_sec = 0
    await asyncio.wait_for(eng.hedge_loop(), timeout=5)
    assert eng.hedger.states["LIT"].delta == 0


async def _no_sleep(_):
    return None
