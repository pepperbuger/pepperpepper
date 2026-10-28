"""미확정 헷지를 주문 최종 상태(accountOrders)로 확정하는 경로.

시간 경과는 근거가 아니다. 주문이 filled 또는 canceled-* 로 끝났고, 포지션이 그 체결 수량만큼
반영된 것을 확인했을 때만 미확정을 풀고, 남은 차이를 다시 헷지한다.
"""

from decimal import Decimal as D

import pytest

from olbot.hedger import Hedger, PendingHedge
from olbot.ledger import Ledger
from olbot.lighter.client import LighterMarket, LighterTrader, OrderStatus, parse_order_status

LIT = LighterMarket("LIT", 120, size_decimals=2, price_decimals=4, min_base=D("1"), min_quote=D("10"))


async def _no_sleep(_):
    return None


class StatusTrader:
    """주문마다 최종 상태 시나리오를 정할 수 있는 가짜 Lighter.

    script: 보낸 순서대로 (최종 상태, 체결 비율, 응답 유실 여부, 상태가 보이기까지 조회 횟수)
    """

    def __init__(self, script):
        self.script = list(script)
        self.pos = D("0")
        self.sends = []
        self.orders = {}  # coi -> [status, filled, lookups_until_visible]
        self.next_coi = 100

    def new_client_order_index(self):
        self.next_coi += 1
        return self.next_coi

    async def positions(self):
        return {120: self.pos}

    async def market_order(self, m, size, is_sell, worst, client_order_index=None):
        status, ratio, lose_response, hidden_lookups = self.script.pop(0)
        filled = (size * D(str(ratio))).quantize(D("0.01"))
        self.sends.append((size, is_sell, worst, client_order_index))
        self.pos += -filled if is_sell else filled
        self.orders[client_order_index] = [status, filled, size, hidden_lookups]
        if lose_response:
            raise ConnectionError("response lost")
        return None

    async def order_status(self, coi):
        rec = self.orders.get(coi)
        if rec is None:
            return None
        if rec[3] > 0:  # 아직 거래소 조회에 안 보임
            rec[3] -= 1
            return None
        return OrderStatus(coi, rec[0], rec[1], rec[2])


def hedger(trader, monkeypatch, saved=None):
    monkeypatch.setattr("olbot.hedger.asyncio.sleep", _no_sleep)
    h = Hedger(trader, {"LIT": LIT}, 15, 3,
               save_pending=(lambda b, p: saved.append((b, p))) if saved is not None else None)
    h.set_ondo_positions({"LIT": D("20")})  # Lighter에서 20개 팔아야 한다
    return h


async def test_lost_response_then_filled_is_confirmed_without_resend(monkeypatch):
    t = StatusTrader([("filled", 1, True, 0)])
    h = hedger(t, monkeypatch)
    await h.hedge("LIT", D("3"))
    assert len(t.sends) == 1
    assert h.states["LIT"].pending is None and h.states["LIT"].delta == 0
    assert h.states["LIT"].consecutive_failures == 0


async def test_not_found_keeps_waiting_and_never_resends(monkeypatch):
    t = StatusTrader([("filled", 1, True, 10_000)])  # 계속 조회에 안 보임
    h = hedger(t, monkeypatch)
    for _ in range(20):
        await h.hedge("LIT", D("3"))
    assert len(t.sends) == 1
    assert h.states["LIT"].pending is not None


async def test_open_order_keeps_waiting(monkeypatch):
    t = StatusTrader([("in-progress", 0, False, 0)])
    h = hedger(t, monkeypatch)
    for _ in range(5):
        await h.hedge("LIT", D("3"))
    assert len(t.sends) == 1 and h.states["LIT"].pending is not None


async def test_unfilled_cancel_is_final_and_retries_wider(monkeypatch):
    # 첫 주문은 가격 초과로 체결 0개 취소 -> 확정 미체결이므로 더 넓은 가격으로 다시 보낸다
    t = StatusTrader([("canceled-too-much-slippage", 0, False, 0), ("filled", 1, False, 0)])
    h = hedger(t, monkeypatch)
    await h.hedge("LIT", D("3"))
    assert [s[0] for s in t.sends] == [D("20"), D("20")]
    assert t.sends[1][2] < t.sends[0][2]  # 매도 최악 가격이 더 낮아짐(더 넓게)
    assert h.states["LIT"].delta == 0 and h.states["LIT"].pending is None


async def test_partial_fill_is_final_and_only_remainder_is_sent(monkeypatch):
    t = StatusTrader([("canceled-not-enough-liquidity", 0.6, False, 0), ("filled", 1, False, 0)])
    h = hedger(t, monkeypatch)
    await h.hedge("LIT", D("3"))
    assert [s[0] for s in t.sends] == [D("20"), D("8.00")]  # 12개 체결 확정 -> 남은 8개만
    assert h.states["LIT"].delta == 0


async def test_terminal_status_waits_until_position_reflects_it(monkeypatch):
    t = StatusTrader([("filled", 1, False, 0)])
    real_positions = t.positions
    calls = {"n": 0}

    async def lagging_positions():
        calls["n"] += 1
        return {120: D("0")} if calls["n"] <= 1 else await real_positions()

    t.positions = lagging_positions
    h = hedger(t, monkeypatch)
    await h.hedge("LIT", D("3"))
    assert h.states["LIT"].pending is not None  # filled라고 나와도 포지션이 안 맞으면 대기
    await h.hedge("LIT", D("3"))
    assert h.states["LIT"].pending is None and len(t.sends) == 1


async def test_order_number_is_saved_before_sending(monkeypatch):
    saved = []
    t = StatusTrader([("filled", 1, True, 0)])
    h = hedger(t, monkeypatch, saved)
    await h.hedge("LIT", D("3"))
    first = saved[0][1]
    assert first is not None and first.client_order_index == t.sends[0][3]


async def test_restored_pending_with_order_number_resolves_after_restart(monkeypatch):
    t = StatusTrader([])
    t.orders[555] = ["filled", D("20"), D("20"), 0]
    t.pos = D("-20")
    monkeypatch.setattr("olbot.hedger.asyncio.sleep", _no_sleep)
    h = Hedger(t, {"LIT": LIT}, 15, 3, initial_pending={"LIT": PendingHedge(D("20"), True, D("0"), 1.0, 555)})
    h.set_ondo_positions({"LIT": D("20")})
    await h.hedge("LIT", D("3"))
    assert h.states["LIT"].pending is None and t.sends == []


def test_ledger_persists_order_number_and_migrates_old_table(tmp_path):
    import sqlite3

    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)  # 이전 버전 스키마 (client_order_index 열 없음) + 남아 있는 미확정 기록
    con.execute("""CREATE TABLE pending_hedges (scope TEXT NOT NULL, base TEXT NOT NULL, size TEXT NOT NULL,
                   is_sell INTEGER NOT NULL, lighter_pos_before TEXT NOT NULL, sent_ts REAL NOT NULL,
                   PRIMARY KEY (scope, base))""")
    con.execute("INSERT INTO pending_hedges VALUES ('s','LIT','20',1,'0',1.0)")
    con.commit()
    con.close()
    led = Ledger(db)
    old = led.load_pending_hedges("s")["LIT"]
    assert old.size == D("20") and old.client_order_index is None  # 예전 기록은 보존
    led.save_pending_hedge("s", "ENA", PendingHedge(D("50"), False, D("1"), 2.0, 777))
    assert led.load_pending_hedges("s")["ENA"].client_order_index == 777


def test_parse_order_status_and_terminal_states():
    o = parse_order_status({"client_order_index": 9, "status": "canceled-too-much-slippage",
                            "filled_base_amount": "0.00", "initial_base_amount": "20.00"})
    assert o.terminal and o.filled == 0
    assert parse_order_status({"client_order_index": 9, "status": "filled",
                               "filled_base_amount": "20", "initial_base_amount": "20"}).terminal
    for s in ("open", "pending", "in-progress"):
        assert not parse_order_status({"client_order_index": 9, "status": s, "filled_base_amount": "0",
                                       "initial_base_amount": "20"}).terminal
    from olbot.lighter.client import CANCELED_STATUSES, OrderStatusError
    for s in CANCELED_STATUSES:  # SDK enum의 취소 상태는 전부 최종
        assert parse_order_status({"client_order_index": 9, "status": s, "filled_base_amount": "0",
                                   "initial_base_amount": "20"}).terminal
    for bad in ({"client_order_index": 9, "status": "filled", "initial_base_amount": "20"},
                {"client_order_index": 9, "status": "canceled-unrecognized-state", "filled_base_amount": "0",
                 "initial_base_amount": "20"},  # 접두어만 맞는 상태는 거부
                {"client_order_index": 9, "status": "canceledx", "filled_base_amount": "0",
                 "initial_base_amount": "20"},
                {"client_order_index": 9, "status": "open"},
                {"status": "filled", "filled_base_amount": "20", "initial_base_amount": "20"}):
        with pytest.raises(OrderStatusError):  # 누락을 0으로 채우지 않는다
            parse_order_status(bad)


async def test_trader_order_status_queries_by_client_index_with_auth(monkeypatch):
    seen = {}

    async def fake_get_json(url, timeout=20.0, headers=None):
        seen["url"], seen["headers"] = url, headers
        return {"code": 200, "orders": [{"client_order_index": 42, "status": "filled",
                                          "filled_base_amount": "20", "initial_base_amount": "20"}]}

    monkeypatch.setattr("olbot.lighter.client.get_json", fake_get_json)
    t = LighterTrader("https://mainnet.zklighter.elliot.ai", 7, 4, "k")

    class FakeSigner:
        calls = 0

        def create_auth_token_with_expiry(self, api_key_index):
            FakeSigner.calls += 1
            assert api_key_index == 4
            return "tok", None

    t.client = FakeSigner()
    st = await t.order_status(42)
    assert st.status == "filled" and st.filled == D("20")
    assert "client_order_indexes=42" in seen["url"] and "account_index=7" in seen["url"]
    assert seen["headers"] == {"authorization": "tok"}
    assert await t.order_status(42) is not None and FakeSigner.calls == 1  # 토큰 재사용


async def test_trader_order_status_not_found_and_error(monkeypatch):
    async def empty(url, timeout=20.0, headers=None):
        return {"code": 200, "orders": []}

    async def bad(url, timeout=20.0, headers=None):
        return {"code": 20001, "message": "invalid auth"}

    t = LighterTrader("https://x", 7, 4, "k")
    t._auth_token, t._auth_token_ts = "tok", 1e18
    monkeypatch.setattr("olbot.lighter.client.get_json", empty)
    assert await t.order_status(1) is None
    monkeypatch.setattr("olbot.lighter.client.get_json", bad)
    with pytest.raises(RuntimeError):
        await t.order_status(1)


def test_pending_cli_list_and_guarded_clear(tmp_path, capsys):
    from pathlib import Path

    import yaml

    from olbot.ledger import pending_scope
    from olbot.main import main

    root = Path(__file__).resolve().parent.parent
    raw = yaml.safe_load((root / "config.live-500.example.yaml").read_text(encoding="utf-8"))
    raw["storage"]["sqlite_path"] = str(tmp_path / "olbot.sqlite")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("LIGHTER_ACCOUNT_INDEX=7\n")
    led = Ledger(tmp_path / "olbot.sqlite")
    led.save_pending_hedge(pending_scope(raw["lighter"]["base_url"], 7), "LIT",
                           PendingHedge(D("20"), True, D("0"), 1.0, 555))
    led.close()
    base = ["--config", str(cfg), "--env", str(env), "pending"]
    assert main(base + ["list"]) == 0
    assert "주문번호 555" in capsys.readouterr().out
    assert main(base + ["clear", "LIT"]) == 1  # --yes 없으면 지우지 않는다
    assert main(base + ["clear", "LIT", "--yes"]) == 0
    assert main(base + ["list"]) == 0
    assert "미확정 헷지 없음" in capsys.readouterr().out


async def test_verify_keys_reports_each_check(monkeypatch):
    from pathlib import Path

    from olbot.config import Secrets, load_config
    from olbot import verify

    cfg = load_config(Path(__file__).resolve().parent.parent / "config.live-500.example.yaml")
    secrets = Secrets("ondoKeyId_a", "ondoApiSecret_b", "0xk", "7", "4")

    async def balance(self):
        return {"marginBalance": "250", "usedMargin": "0"}

    async def positions(self):
        raise RuntimeError("HTTP 401 ip_not_permitted")

    async def login(url, key_id, secret, timeout=10.0):
        return "ok"

    monkeypatch.setattr(verify.OndoRest, "balance", balance)
    monkeypatch.setattr(verify.OndoRest, "positions", positions)
    monkeypatch.setattr(verify, "_ondo_ws_login", login)
    monkeypatch.setattr("olbot.lighter.client.LighterTrader", _verify_trader([_REAL_LIKE]))
    results = await verify.verify_keys(cfg, secrets)
    by = {n: ok for n, ok, _ in results}
    assert by["Ondo REST 잔고"] and not by["Ondo REST 포지션"] and by["Ondo WS 로그인"]
    assert by["Lighter API 키 등록"] and by["Lighter 주문 조회 인증"]
    assert by["Lighter 주문 응답 형식"] and by["Lighter 주문 번호 조회"]


_REAL_LIKE = {"client_order_index": 42, "status": "canceled-too-much-slippage",
              "filled_base_amount": "12.00", "initial_base_amount": "20.00", "market_index": 120}


def _verify_trader(recent, by_coi=None):
    """verify-keys용 가짜 Lighter. recent: 과거 주문 원본, by_coi: 주문 번호 조회가 돌려줄 원본(기본은 recent에서 찾음)."""

    class T:
        def __init__(self, *a):
            pass

        async def connect(self):
            pass

        async def account(self):
            return {"total_asset_value": "250", "available_balance": "250"}

        async def recent_orders(self, limit=5):
            return list(recent)

        async def order_status(self, coi):
            src = by_coi if by_coi is not None else {o.get("client_order_index"): o for o in recent}
            raw = src.get(coi)
            return None if raw is None else parse_order_status(raw)

        async def close(self):
            pass

    return T


async def _lighter_checks(monkeypatch, trader_cls):
    from olbot import verify
    from olbot.config import Secrets
    from olbot.lighter.client import LighterTrader  # noqa: F401  (monkeypatch 대상 모듈 로드)

    class Cfg:
        class ondo:
            rest_url = "https://x"
            ws_url = "wss://x"

        class lighter:
            base_url = "https://mainnet.zklighter.elliot.ai"

    async def ok(*a, **k):
        return {}

    monkeypatch.setattr(verify.OndoRest, "balance", ok)
    monkeypatch.setattr(verify.OndoRest, "positions", lambda self: ok())
    monkeypatch.setattr(verify, "_ondo_ws_login", lambda *a, **k: ok())
    monkeypatch.setattr("olbot.lighter.client.LighterTrader", trader_cls)
    results = await verify.verify_keys(Cfg, Secrets("ondoKeyId_a", "ondoApiSecret_b", "0xk", "7", "4"))
    return {n: (ok_, d) for n, ok_, d in results}


async def test_verify_keys_without_order_history_does_not_pass(monkeypatch):
    # 빈 목록 조회는 파서를 타지 않는다. 과거 주문이 없으면 형식 확인을 통과로 치지 않는다
    by = await _lighter_checks(monkeypatch, _verify_trader([]))
    assert by["Lighter 주문 조회 인증"][0]
    ok, detail = by["Lighter 주문 응답 형식"]
    assert not ok and "기록이 없어" in detail


async def test_verify_keys_reports_real_record_the_parser_rejects(monkeypatch):
    raw = dict(_REAL_LIKE)
    del raw["filled_base_amount"]
    raw["filledBaseAmount"] = "12.00"  # 실제 필드 이름이 다르면 여기서 드러나야 한다
    by = await _lighter_checks(monkeypatch, _verify_trader([raw]))
    ok, detail = by["Lighter 주문 응답 형식"]
    assert not ok and "filled_base_amount 누락" in detail and "filledBaseAmount" in detail
    assert "Lighter 주문 번호 조회" not in by


async def test_verify_keys_unknown_cancel_status_is_reported(monkeypatch):
    raw = dict(_REAL_LIKE, status="canceled-unrecognized-state")
    by = await _lighter_checks(monkeypatch, _verify_trader([raw]))
    assert not by["Lighter 주문 응답 형식"][0]


async def test_verify_keys_lookup_by_client_index_must_match_history(monkeypatch):
    other = dict(_REAL_LIKE, filled_base_amount="0.00")
    by = await _lighter_checks(monkeypatch, _verify_trader([_REAL_LIKE], by_coi={42: other}))
    assert by["Lighter 주문 응답 형식"][0]
    assert not by["Lighter 주문 번호 조회"][0]
    by = await _lighter_checks(monkeypatch, _verify_trader([_REAL_LIKE], by_coi={}))
    assert not by["Lighter 주문 번호 조회"][0]  # 기록엔 있는데 번호 조회로 못 찾음


async def test_verify_keys_order_without_client_index_is_not_a_pass(monkeypatch):
    raw = dict(_REAL_LIKE, client_order_index=0)
    by = await _lighter_checks(monkeypatch, _verify_trader([raw]))
    assert by["Lighter 주문 응답 형식"][0]
    assert not by["Lighter 주문 번호 조회"][0]


async def test_trader_recent_orders_uses_inactive_orders_with_auth(monkeypatch):
    seen = {}

    async def fake_get_json(url, timeout=20.0, headers=None):
        seen["url"], seen["headers"] = url, headers
        return {"code": 200, "orders": [_REAL_LIKE]}

    monkeypatch.setattr("olbot.lighter.client.get_json", fake_get_json)
    t = LighterTrader("https://mainnet.zklighter.elliot.ai", 7, 4, "k")
    t._auth = lambda: "tok"
    assert await t.recent_orders(3) == [_REAL_LIKE]
    assert seen["url"].endswith("/api/v1/accountInactiveOrders?account_index=7&limit=3")
    assert seen["headers"] == {"authorization": "tok"}


# ---------- 87fa32b 검토: 필드 누락, 1단위 오차 허용, 나중에 확인된 취소의 실패 누락 ----------

class RawStatusTrader(StatusTrader):
    """order_status가 거래소 원본 응답(dict)을 파서로 넘기는 경로를 그대로 탄다."""

    def __init__(self, raw_for_coi):
        super().__init__([])
        self.raw_for_coi = raw_for_coi

    async def market_order(self, m, size, is_sell, worst, client_order_index=None):
        self.sends.append((size, is_sell, worst, client_order_index))
        # 실제로는 전량 체결됐지만 포지션 조회에는 아직 반영되지 않은 상황 (가장 위험한 경우)
        return None

    async def order_status(self, coi):
        return parse_order_status(self.raw_for_coi(coi))


@pytest.mark.parametrize("raw", [
    {"status": "filled", "initial_base_amount": "20.00"},                       # filled 수량 누락
    {"status": "filled", "filled_base_amount": "20.00"},                        # 주문 수량 누락
    {"filled_base_amount": "20.00", "initial_base_amount": "20.00"},            # 상태 누락
    {"status": "filled", "filled_base_amount": "abc", "initial_base_amount": "20.00"},
    {"status": "filled", "filled_base_amount": "12.00", "initial_base_amount": "20.00"},  # filled인데 일부만
    {"status": "canceled", "filled_base_amount": "25.00", "initial_base_amount": "20.00"},  # 체결 > 주문
    {"status": "filled", "filled_base_amount": "30.00", "initial_base_amount": "30.00"},  # 보낸 수량과 다름
    {"status": "mystery", "filled_base_amount": "0", "initial_base_amount": "20.00"},     # 모르는 상태
    {"status": "canceled-unrecognized-state", "filled_base_amount": "0",                 # 목록에 없는 취소
     "initial_base_amount": "20.00"},
])
async def test_malformed_or_inconsistent_status_keeps_pending(monkeypatch, raw):
    """보낸 수량은 20. 응답이 이상하면 포지션 반영 전이어도 미체결로 확정하지 않는다."""
    t = RawStatusTrader(lambda coi: {"client_order_index": coi, **raw})
    h = hedger(t, monkeypatch)
    for _ in range(3):
        await h.hedge("LIT", D("3"))
    assert len(t.sends) == 1  # 잘못된 응답을 미체결로 확정해 다시 보내면 안 된다
    assert h.states["LIT"].pending is not None


async def test_one_unit_unreflected_fill_is_not_accepted(monkeypatch):
    # 0.01개 체결 후 취소됐는데 포지션 조회에는 아직 0으로 보임 -> 통과시키면 20개를 다시 보낸다
    t = StatusTrader([("canceled-too-much-slippage", 0, False, 0), ("filled", 1, False, 0)])
    real_order = t.market_order

    async def tiny_fill_hidden(m, size, is_sell, worst, client_order_index=None):
        await real_order(m, size, is_sell, worst, client_order_index)
        if len(t.sends) == 1:
            t.orders[client_order_index][1] = D("0.01")  # 거래소 기록: 0.01 체결
        return None

    t.market_order = tiny_fill_hidden
    h = hedger(t, monkeypatch)
    await h.hedge("LIT", D("3"))
    assert len(t.sends) == 1 and h.states["LIT"].pending is not None  # 반영 전에는 대기
    t.pos = D("-0.01")  # 포지션 반영됨
    await h.hedge("LIT", D("3"))
    assert [s[0] for s in t.sends] == [D("20"), D("19.99")]  # 잔량만


async def test_cancel_confirmed_on_later_reconcile_counts_failure_and_widens(monkeypatch):
    # 취소 결과가 첫 확인에는 안 보이고 다음 대사에서 확인되는 경우가 반복된다
    t = StatusTrader([("canceled-too-much-slippage", 0, False, 1)] * 4)
    h = hedger(t, monkeypatch)
    for _ in range(8):
        await h.hedge("LIT", D("3"))
        if len(t.sends) == 4:
            break
    worsts = [s[2] for s in t.sends]
    assert worsts[1] < worsts[0] and worsts[2] < worsts[1]  # 매도 최악 가격이 점점 넓어진다
    assert h.states["LIT"].consecutive_failures >= 3  # 위험 판단이 kill할 수 있다


async def test_exchange_size_matches_rounded_down_sent_size(monkeypatch):
    # Ondo 차이가 19.996개면 Lighter에는 19.99개로 내림해서 보낸다. 그 주문은 정상 확정돼야 한다
    t = StatusTrader([("filled", 1, False, 0)])

    async def mo(m, size, is_sell, worst, client_order_index=None):
        sent = D(m.to_int_size(size)).scaleb(-m.size_decimals)
        t.sends.append((size, is_sell, worst, client_order_index))
        t.pos += -sent if is_sell else sent
        t.orders[client_order_index] = ["filled", sent, sent, 0]
        return None

    t.market_order = mo
    h = hedger(t, monkeypatch)
    h.set_ondo_positions({"LIT": D("19.996")})
    await h.hedge("LIT", D("3"))
    assert h.states["LIT"].pending is None and len(t.sends) == 1
