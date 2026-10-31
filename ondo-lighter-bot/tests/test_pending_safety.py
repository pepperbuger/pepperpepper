"""미확정 주문은 시간 경과/일부 체결/재시작으로 해제되면 안 된다. 모의 거래소만 사용."""

import asyncio
from decimal import Decimal as D

import pytest

from olbot.hedger import Hedger, PendingHedge
from olbot.ledger import Ledger
from test_engine_safety import LIT, LaggingTrader, _no_sleep, _risk_engine


@pytest.mark.parametrize("lost_response", [True, False])
@pytest.mark.parametrize("step", [11, 31, 600])
async def test_long_delays_never_resend_accepted_order(monkeypatch, lost_response, step):
    clock = {"t": 1000.0}
    monkeypatch.setattr("olbot.hedger.time.time", lambda: clock["t"])
    monkeypatch.setattr("olbot.hedger.asyncio.sleep", _no_sleep)
    trader = LaggingTrader(raise_on_send=lost_response, lag_reads=3)
    h = Hedger(trader, {"LIT": LIT}, 15, 3)
    h.set_ondo_positions({"LIT": D("20")})
    await h.hedge("LIT", D("3"))
    for _ in range(4):
        clock["t"] += step
        await h.refresh_lighter()  # 실제 Engine의 대사 순서: 조회 후 hedge
        await h.hedge("LIT", D("3"))
        assert trader.sends == [(D("20"), True)]
    assert trader.real == D("-20") and h.states["LIT"].delta == 0
    assert h.states["LIT"].pending is None


async def test_partial_fill_keeps_original_order_pending(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr("olbot.hedger.time.time", lambda: clock["t"])
    monkeypatch.setattr("olbot.hedger.asyncio.sleep", _no_sleep)
    trader = LaggingTrader(raise_on_send=True, lag_reads=10000)
    h = Hedger(trader, {"LIT": LIT}, 15, 3)
    h.set_ondo_positions({"LIT": D("20")})
    await h.hedge("LIT", D("3"))
    original = h.states["LIT"].pending
    trader.visible = D("-10")
    clock["t"] += 600
    await h.hedge("LIT", D("3"))
    assert trader.sends == [(D("20"), True)]
    assert h.states["LIT"].pending is original
    trader.visible = D("-20")
    await h.hedge("LIT", D("3"))
    assert h.states["LIT"].pending is None
    assert len(trader.sends) == 1


async def test_overdue_pending_pauses_and_pulls_all_quotes_then_resumes(monkeypatch):
    clock = {"t": 1000.0}
    eng = _risk_engine(monkeypatch, clock)
    eng.hedger.states["LIT"].pending = PendingHedge(D("20"), True, D("0"), clock["t"])
    # 노출이 0이어도 미확정 자체로 호가를 멈춘다.
    clock["t"] += 11
    await eng._risk_tick()
    assert eng.paused and "미확정" in eng.paused
    for base in eng.bases:
        await eng._quote_market(base, clock["t"])
    assert all(eng.current[b] is None for b in eng.bases)
    assert set(eng.ondo_rest.cancel_calls) == {"LIT-USD.P", "ENA-USD.P"}
    # 원래 주문 수량 전체가 조회에 반영된 뒤에만 대기와 일시정지를 해제한다.
    eng.hedger.states["LIT"].ondo_pos = D("20")
    eng.hedger.trader.pos[120] = D("-20")
    await eng.hedger.hedge("LIT", D("3"))
    eng.positions_ok_ts = clock["t"]
    await eng._risk_tick()
    assert eng.paused is None and eng.killed is None
    eng.ledger.close()


async def test_quote_guard_blocks_before_next_risk_tick(monkeypatch):
    clock = {"t": 1000.0}
    eng = _risk_engine(monkeypatch, clock)
    eng.board.update_ondo("ENA", D("2.999"), D("3.001"), clock["t"])
    eng.board.update_lighter("ENA", D("2.999"), D("3.001"), clock["t"])
    eng.hedger.states["LIT"].pending = PendingHedge(D("20"), True, D("0"), clock["t"] - 11)
    assert eng.paused is None  # 위험 루프가 아직 실행되지 않은 상황
    await eng._quote_market("ENA", clock["t"])
    assert eng.current["ENA"] is None
    assert eng.ondo_rest.cancel_calls == ["ENA-USD.P"]
    eng.ledger.close()


async def test_pause_during_cancel_prevents_new_quotes(monkeypatch):
    from test_engine_safety import QUOTES

    clock = {"t": 1000.0}
    eng = _risk_engine(monkeypatch, clock)
    eng.hedger.states["LIT"].pending = PendingHedge(D("20"), True, D("0"), clock["t"])

    class SlowCancel:
        creates = 0

        async def cancel_all(self, market):
            clock["t"] += 11

        async def create_batch(self, orders):
            self.creates += 1
            return [], []

    eng.ondo_rest = SlowCancel()
    await eng.place("ENA", QUOTES)
    assert eng.ondo_rest.creates == 0 and eng.current["ENA"] is None
    eng.ledger.close()


async def test_hedge_loop_resolves_pending_even_when_delta_is_zero(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr("olbot.hedger.asyncio.sleep", _no_sleep)
    eng = _risk_engine(monkeypatch, clock)
    eng.bases = ["LIT"]
    eng.hedger.states["LIT"].ondo_pos = D("20")
    eng.hedger.states["LIT"].lighter_pos = D("-20")
    eng.hedger.states["LIT"].pending = PendingHedge(D("20"), True, D("0"), clock["t"])
    eng.hedger.trader.pos[120] = D("-20")

    async def reconcile():
        eng.stop.set()

    eng.reconcile_positions = reconcile
    eng.cfg.hedging.reconcile_interval_sec = 0
    await asyncio.wait_for(eng.hedge_loop(), timeout=1)
    assert eng.hedger.states["LIT"].pending is None
    assert eng.hedger.trader.calls == 0
    eng.ledger.close()


async def test_pending_survives_restart_and_is_scoped_to_account(tmp_path, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr("olbot.hedger.time.time", lambda: clock["t"])
    monkeypatch.setattr("olbot.hedger.asyncio.sleep", _no_sleep)
    path = tmp_path / "ledger.sqlite"
    scope = "https://testnet.zklighter.elliot.ai:account:1"
    trader = LaggingTrader(raise_on_send=True, lag_reads=10000)
    led = Ledger(path)
    h = Hedger(trader, {"LIT": LIT}, 15, 3,
               save_pending=lambda base, p: led.save_pending_hedge(scope, base, p))
    h.set_ondo_positions({"LIT": D("20")})
    await h.hedge("LIT", D("3"))
    led.close()

    led = Ledger(path)
    assert led.load_pending_hedges(scope + "-different") == {}
    h = Hedger(trader, {"LIT": LIT}, 15, 3,
               initial_pending=led.load_pending_hedges(scope),
               save_pending=lambda base, p: led.save_pending_hedge(scope, base, p))
    h.set_ondo_positions({"LIT": D("20")})
    clock["t"] += 3600
    await h.hedge("LIT", D("3"))
    assert trader.sends == [(D("20"), True)]
    assert h.overdue_pending(clock["t"]) == ("LIT",)
    trader.visible = D("-20")
    await h.hedge("LIT", D("3"))
    assert led.load_pending_hedges(scope) == {}
    led.close()


async def test_persist_pending_before_send_and_delete_after_full_fill(tmp_path, monkeypatch):
    monkeypatch.setattr("olbot.hedger.asyncio.sleep", _no_sleep)
    led = Ledger(tmp_path / "ledger.sqlite")
    scope = "testnet:account:1"

    class CheckedTrader(LaggingTrader):
        async def market_order(self, m, size, is_sell, worst):
            assert led.load_pending_hedges(scope)["LIT"].size == size
            return await super().market_order(m, size, is_sell, worst)

    trader = CheckedTrader(raise_on_send=False, lag_reads=0)
    h = Hedger(trader, {"LIT": LIT}, 15, 3,
               save_pending=lambda base, p: led.save_pending_hedge(scope, base, p))
    h.set_ondo_positions({"LIT": D("20")})
    await h.hedge("LIT", D("3"))
    assert h.states["LIT"].delta == 0 and led.load_pending_hedges(scope) == {}
    led.close()


async def test_failed_pending_write_prevents_send(monkeypatch):
    monkeypatch.setattr("olbot.hedger.asyncio.sleep", _no_sleep)
    trader = LaggingTrader(raise_on_send=False, lag_reads=0)

    def broken_save(base, pending):
        raise OSError("simulated disk full")

    h = Hedger(trader, {"LIT": LIT}, 15, 3, save_pending=broken_save)
    h.set_ondo_positions({"LIT": D("20")})
    await h.hedge("LIT", D("3"))
    assert trader.sends == []


def test_cannot_drop_unresolved_market_from_config():
    with pytest.raises(ValueError, match="미확정"):
        Hedger(object(), {}, 15, 3, initial_pending={"LIT": PendingHedge(D("20"), True, D("0"), 1000)})
