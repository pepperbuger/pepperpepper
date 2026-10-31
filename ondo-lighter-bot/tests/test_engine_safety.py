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


async def test_hedger_survives_order_exception_and_keeps_pending(monkeypatch):
    """응답 유실은 '모름'이다. 시간 경과로 실패/미체결을 확정하지 않는다."""
    clock = {"t": 1000.0}
    monkeypatch.setattr("olbot.hedger.asyncio.sleep", _no_sleep)
    monkeypatch.setattr("olbot.hedger.time.time", lambda: clock["t"])
    trader = ExplodingTrader()
    h = Hedger(trader, {"LIT": LIT}, 15, 3)
    h.set_ondo_positions({"LIT": D("20")})
    await h.hedge("LIT", D("3"))  # 예외가 밖으로 나오면 안 된다
    assert trader.calls == 1 and h.states["LIT"].pending is not None
    for _ in range(3):
        clock["t"] += 11
        await h.hedge("LIT", D("3"))
    assert trader.calls == 1
    assert h.states["LIT"].consecutive_failures == 0
    assert h.states["LIT"].pending is not None


async def test_hedge_loop_keeps_running_when_lighter_raises(monkeypatch):
    """검토에서 재현된 경로: Lighter 주문 중 예외 -> 헷지 루프가 죽으면 안 된다."""
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
    assert trader.calls == 1  # 결과를 모르는 주문이 있는 동안 다시 보내지 않았다
    assert eng.hedger.states["LIT"].pending is not None


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


# ---------- 2차 검토 (0a36f84): 미확정 헷지 중복 전송, 조회 장애 시 노출 정지 ----------

class LaggingTrader:
    """주문은 접수되지만 응답을 잃거나(예외) 포지션 반영이 늦는 Lighter."""

    def __init__(self, raise_on_send: bool, lag_reads: int) -> None:
        self.real = D("0")          # 거래소의 실제 포지션
        self.visible = D("0")       # 조회로 보이는 포지션 (늦게 따라온다)
        self.raise_on_send = raise_on_send
        self.lag_reads = lag_reads
        self.reads_since_send = 0
        self.sends = []

    async def positions(self):
        self.reads_since_send += 1
        if self.reads_since_send > self.lag_reads:
            self.visible = self.real
        return {120: self.visible}

    async def market_order(self, m, size, is_sell, worst):
        self.sends.append((size, is_sell))
        self.real += -size if is_sell else size
        self.reads_since_send = 0
        if self.raise_on_send:
            raise ConnectionError("response lost")
        return None


async def _repeat_hedge(h, times, clock, step):
    for _ in range(times):
        await h.hedge("LIT", D("3"))
        clock["t"] += step


async def test_lost_response_order_is_not_resent_while_unconfirmed(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr("olbot.hedger.asyncio.sleep", _no_sleep)
    monkeypatch.setattr("olbot.hedger.time.time", lambda: clock["t"])
    trader = LaggingTrader(raise_on_send=True, lag_reads=3)
    h = Hedger(trader, {"LIT": LIT}, 15, 3)
    h.set_ondo_positions({"LIT": D("20")})
    await _repeat_hedge(h, 5, clock, 2.0)  # 대사 루프가 2초마다 다시 부르는 상황
    assert trader.sends == [(D("20"), True)]  # 매도 20개 한 번만
    assert h.states["LIT"].delta == 0
    assert h.states["LIT"].pending is None


async def test_accepted_but_lagging_position_is_not_resent(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr("olbot.hedger.asyncio.sleep", _no_sleep)
    monkeypatch.setattr("olbot.hedger.time.time", lambda: clock["t"])
    trader = LaggingTrader(raise_on_send=False, lag_reads=2)
    h = Hedger(trader, {"LIT": LIT}, 15, 3)
    h.set_ondo_positions({"LIT": D("20")})
    await _repeat_hedge(h, 5, clock, 2.0)
    assert trader.sends == [(D("20"), True)]
    assert h.states["LIT"].delta == 0


async def test_unconfirmed_order_never_expires_without_final_evidence(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr("olbot.hedger.asyncio.sleep", _no_sleep)
    monkeypatch.setattr("olbot.hedger.time.time", lambda: clock["t"])

    class NeverFills(LaggingTrader):
        async def market_order(self, m, size, is_sell, worst):
            self.sends.append((size, is_sell))
            return None  # 접수됐지만 IOC가 체결 없이 사라짐

    trader = NeverFills(raise_on_send=False, lag_reads=0)
    h = Hedger(trader, {"LIT": LIT}, 15, 3)
    h.set_ondo_positions({"LIT": D("20")})
    await h.hedge("LIT", D("3"))
    assert len(trader.sends) == 1 and h.states["LIT"].pending is not None
    clock["t"] += 5
    await h.hedge("LIT", D("3"))
    assert len(trader.sends) == 1  # 확인 대기 중에는 보내지 않는다
    clock["t"] += 3600  # 체결이 안 보여도 미체결/만료를 확정할 수 없다
    await h.hedge("LIT", D("3"))
    assert len(trader.sends) == 1
    assert h.states["LIT"].pending is not None
    assert h.states["LIT"].consecutive_failures == 0


class _WS:
    connected = True
    ready = True

    def stale_for(self, now=None):
        return 0.0


def _risk_engine(monkeypatch, clock):
    monkeypatch.setattr("olbot.engine.time.time", lambda: clock["t"])
    eng = make_engine(FlakyOndo(fail_times=0))
    eng.ondo_ws = eng.lighter_ws = eng.private_ws = _WS()
    eng.lighter_markets = {b: LIT for b in eng.bases}
    eng.hedger = Hedger(ExplodingTrader(), eng.lighter_markets, 15, 3)
    eng.board.update_lighter("LIT", D("2.999"), D("3.001"), clock["t"])
    eng.board.update_ondo("LIT", D("2.999"), D("3.001"), clock["t"])
    eng.positions_ok_ts = clock["t"]
    return eng


async def test_unhedged_exposure_kills_even_when_position_reads_fail(monkeypatch):
    clock = {"t": 1000.0}
    eng = _risk_engine(monkeypatch, clock)
    eng.hedger.states["LIT"].ondo_pos = D("20")  # 체결로 생긴 60달러 노출, 대사는 계속 실패 중
    for _ in range(25):
        await eng._risk_tick()
        clock["t"] += 1
    assert eng.hedger.states["LIT"].unhedged_since is not None
    assert eng.killed and "LIT" in eng.killed


async def test_stale_positions_pause_quoting(monkeypatch):
    clock = {"t": 1000.0}
    eng = _risk_engine(monkeypatch, clock)
    clock["t"] += 31  # 31초 동안 포지션 조회 성공 없음
    await eng._risk_tick()
    assert eng.paused and "포지션" in eng.paused
    eng.positions_ok_ts = clock["t"]
    await eng._risk_tick()
    assert eng.paused is None
