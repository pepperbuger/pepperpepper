import hashlib
import hmac
import time
from decimal import Decimal as D
from pathlib import Path

import pytest

from olbot.config import load_config, load_secrets, validate, validate_live
from olbot.hedger import Hedger, hedge_order
from olbot.ledger import Ledger
from olbot.lighter.client import LighterMarket, parse_account_positions
from olbot.ondo.private_ws import OndoPrivateWS
from olbot.ondo.rest import sign, signed_position, ws_login_message
from olbot.quoter import QuoteSide, TargetQuotes, needs_requote, target_quotes, to_order_payloads
from olbot.ratelimit import TokenBucket
from olbot.report import build_report
from olbot.risk import RiskInputs, evaluate

ROOT = Path(__file__).resolve().parent.parent
SOL = LighterMarket("SOL", 2, size_decimals=3, price_decimals=3, min_base=D("0.055"), min_quote=D("10"))


# ---------- Ondo 서명 ----------

def test_rest_signature_matches_documented_scheme():
    # 문서 예시: timestamp + METHOD + path(쿼리 포함) + body 를 secret으로 HMAC-SHA256
    path = "/v1/perps/orders?market=AAPL-USD.P&limit=1000"
    expected = hmac.new(b"ondoApiSecret_SECRET", ("1700000000000GET" + path).encode(), hashlib.sha256).hexdigest()
    assert sign("ondoApiSecret_SECRET", "1700000000000", "get", path, "") == expected


def test_ws_login_message():
    msg = ws_login_message("ondoKeyId_X", "s3cret", now_ms=1700000000000)
    want = hmac.new(b"s3cret", b"1700000000000ondo_perps_ws_login", hashlib.sha256).hexdigest()
    assert msg == {"op": "login", "args": {"key": "ondoKeyId_X", "time": "1700000000000", "sign": want}}


def test_signed_position_uses_direction():
    assert signed_position({"direction": "short", "netQuantity": "1.5"}) == D("-1.5")
    assert signed_position({"direction": "short", "netQuantity": "-1.5"}) == D("-1.5")
    assert signed_position({"direction": "long", "netQuantity": "2"}) == D("2")
    assert signed_position({"direction": "neutral", "netQuantity": "0"}) == D("0")


# ---------- Lighter ----------

def test_lighter_integer_conversion_and_min_hedge():
    assert SOL.to_int_size(D("0.1239")) == 123
    assert SOL.to_int_price(D("100.1259")) == 100125
    # 최소 10 USD x 1.05 / 100 = 0.105 SOL > 최소 수량 0.055
    assert SOL.min_hedge_size(D("100")) == D("0.105")


def test_parse_lighter_account_positions():
    acct = {"positions": [
        {"market_id": 2, "sign": -1, "position": "0.500"},
        {"market_id": 1, "sign": 1, "position": "0.010"},
        {"market_id": 3, "sign": 0, "position": "0"},
    ]}
    assert parse_account_positions(acct) == {2: D("-0.500"), 1: D("0.010"), 3: D("0")}


# ---------- 호가 ----------

def q(**kw):
    base = dict(fair=D("100"), offset_bps=18, size_usd=D("60"), inventory_base=D("0"), max_inventory_usd=D("200"),
                skew_bps_per_1k=20, price_increment=D("0.01"), size_increment=D("0.01"))
    base.update(kw)
    return target_quotes(**base)


def test_target_quotes_symmetric_when_flat():
    t = q()
    assert t.bid == QuoteSide(D("99.82"), D("0.60"))
    assert t.ask == QuoteSide(D("100.18"), D("0.60"))


def test_long_inventory_skews_quotes_down():
    t = q(inventory_base=D("1"))  # 재고 100 USD -> 2bps 아래로
    assert t.bid.price == D("99.80")
    assert t.ask.price == D("100.16")


def test_inventory_limit_removes_increasing_side():
    t = q(inventory_base=D("2"))  # 200 USD = 한도
    assert t.bid is None and t.ask is not None
    t = q(inventory_base=D("-2"))
    assert t.ask is None and t.bid is not None


def test_too_small_size_quotes_nothing():
    t = q(size_usd=D("5"), size_increment=D("1"))  # 0.05개 -> 단위 1로 내리면 0
    assert t.bid is None and t.ask is None
    t = q(min_size=D("1"))  # Lighter 최소 헷지 수량보다 작으면 걸지 않는다
    assert t.bid is None and t.ask is None


def test_needs_requote():
    mid = D("100")
    t = q()
    assert needs_requote(None, t, mid, 25, 6)
    assert not needs_requote(t, t, mid, 25, 6)
    moved = q(fair=D("100.10"))  # 10bps 이동 > 6
    assert needs_requote(t, moved, D("100.10"), 25, 6)
    small = q(fair=D("100.03"))  # 3bps 이동
    assert not needs_requote(t, small, D("100.03"), 25, 6)
    assert needs_requote(t, t, D("100.30"), 25, 6)  # 중간가가 30bps 움직여 band 밖
    assert needs_requote(t, TargetQuotes(t.bid, None), mid, 25, 6)
    bigger = q(size_usd=D("90"))  # 수량 50% 차이
    assert needs_requote(t, bigger, mid, 25, 6)


def test_order_payloads_are_post_only_strings():
    orders = to_order_payloads("SOL-USD.P", q())
    assert [o["side"] for o in orders] == ["buy", "sell"]
    assert all(o["postOnly"] is True and o["timeInForce"] == "GTC" for o in orders)
    assert D(orders[0]["price"]) == D("99.82") and D(orders[0]["size"]) == D("0.6")
    assert "E" not in orders[0]["price"] + orders[0]["size"]  # 지수 표기 없이 보낸다


# ---------- 헷지 ----------

def test_hedge_order_direction_and_minimum():
    assert hedge_order(D("0.05"), SOL, D("100"), 15) is None  # 최소 미만
    size, is_sell, worst = hedge_order(D("0.6"), SOL, D("100"), 15)
    assert (size, is_sell, worst) == (D("0.6"), True, D("99.85"))
    size, is_sell, worst = hedge_order(D("-0.6"), SOL, D("100"), 15)
    assert (size, is_sell, worst) == (D("0.6"), False, D("100.15"))


class FakeTrader:
    def __init__(self, fill=True, err=None):
        self.pos = {2: D("0")}
        self.fill = fill
        self.err = err
        self.orders = []

    async def positions(self):
        return dict(self.pos)

    async def market_order(self, m, size, is_sell, worst):
        self.orders.append((size, is_sell, worst))
        if self.err:
            return self.err
        if self.fill:
            self.pos[m.market_id] += -size if is_sell else size
        return None


async def test_hedger_neutralizes_ondo_fill(monkeypatch):
    monkeypatch.setattr("olbot.hedger.asyncio.sleep", _no_sleep)
    rec = []
    h = Hedger(FakeTrader(), {"SOL": SOL}, 15, 3, record=lambda *a: rec.append(a))
    h.set_ondo_positions({"SOL": D("0.6")})
    await h.hedge("SOL", D("100"))
    assert h.states["SOL"].delta == 0
    assert h.trader.orders == [(D("0.6"), True, D("99.85"))]
    assert h.states["SOL"].consecutive_failures == 0
    assert len(rec) == 1


async def test_hedger_does_not_retry_blind_when_refresh_fails(monkeypatch):
    monkeypatch.setattr("olbot.hedger.asyncio.sleep", _no_sleep)
    trader = FakeTrader()
    h = Hedger(trader, {"SOL": SOL}, 15, 3)
    h.set_ondo_positions({"SOL": D("0.6")})

    async def broken():
        raise OSError("network")
    trader.positions = broken
    await h.hedge("SOL", D("100"))
    assert len(trader.orders) == 1  # 체결 여부를 모르면 두 번째 주문을 보내지 않는다


def test_rest_snapshot_skips_recently_filled_market():
    h = Hedger(FakeTrader(), {"SOL": SOL}, 15, 3)
    h.states["SOL"].ondo_pos = D("0.6")  # 방금 체결로 올린 값
    h.set_ondo_positions({}, skip={"SOL"})  # 체결 전 스냅샷
    assert h.states["SOL"].ondo_pos == D("0.6")
    h.set_ondo_positions({})
    assert h.states["SOL"].ondo_pos == D("0")


async def test_hedger_retries_with_wider_slippage_then_counts_failure(monkeypatch):
    monkeypatch.setattr("olbot.hedger.asyncio.sleep", _no_sleep)
    h = Hedger(FakeTrader(fill=False), {"SOL": SOL}, 15, 3)
    h.set_ondo_positions({"SOL": D("-0.6")})
    await h.hedge("SOL", D("100"))
    worsts = [o[2] for o in h.trader.orders]
    assert worsts == [D("100.15"), D("100.30"), D("100.45")]
    assert h.states["SOL"].consecutive_failures == 1


async def _no_sleep(_):
    return None


# ---------- 위험 ----------

def risk(**kw):
    base = dict(now=1000.0, unhedged_usd={"SOL": D("0")}, unhedged_since={"SOL": None}, hedge_failures={"SOL": 0},
                public_ws_silent_sec=0.0, private_ws_up=True, equity_start=D("500"), equity_now=D("500"),
                ondo_margin_usage=0.1)
    base.update(kw)
    return evaluate(RiskInputs(**base), D("40"), 20, 10, D("25"), 0.6)


def test_risk_ok_by_default():
    d = risk()
    assert d.kill_reason is None and d.pause_reason is None


def test_risk_kills_on_long_unhedged_exposure():
    assert risk(unhedged_usd={"SOL": D("60")}, unhedged_since={"SOL": 975.0}).kill_reason
    assert risk(unhedged_usd={"SOL": D("60")}, unhedged_since={"SOL": 995.0}).kill_reason is None  # 아직 20초 안 됨
    assert risk(unhedged_usd={"SOL": D("30")}, unhedged_since={"SOL": 900.0}).kill_reason is None  # 한도 이하


def test_risk_kills_on_loss_and_hedge_failures():
    assert risk(equity_now=D("470")).kill_reason
    assert risk(hedge_failures={"SOL": 3}).kill_reason


def test_risk_pauses_on_ws_and_margin():
    assert risk(private_ws_up=False).pause_reason
    assert risk(public_ws_silent_sec=30).pause_reason
    assert risk(ondo_margin_usage=0.9).pause_reason


# ---------- 비공개 WS ----------

def test_private_ws_routes_fills_and_positions():
    got = []
    ws = OndoPrivateWS("wss://x", "k", "s", ["SOL-USD.P"], 30, 10, got.append)
    subs = ws.subscriptions()
    assert [s["op"] for s in subs] == ["login"]  # 비공개 구독은 로그인 확인 뒤에
    sent = []
    ws.send_nowait = sent.append
    ws.connected = True
    ws.on_message({"type": "loggedIn"}, time.time())
    assert ws.logged_in and not ws.ready
    assert {"op": "subscribe", "channel": "cancelAllOrdersAfterPerps", "timeout_seconds": 30} in sent
    ws.on_message({"type": "subscribed", "channel": "cancelAllOrdersAfterPerps"}, time.time())
    assert ws.ready
    fill = {"id": "f1", "market": "SOL-USD.P", "side": "buy", "price": "99.8", "size": "0.6", "isMaker": True}
    ws.on_message({"type": "update", "channel": "fillsPerps", "data": [fill]}, time.time())
    assert got == [fill]
    ws.on_message({"type": "update", "channel": "positionsPerps",
                   "data": [{"market": "SOL-USD.P", "direction": "long", "netQuantity": "0.6"}]}, time.time())
    assert ws.positions == {"SOL-USD.P": D("0.6")}


# ---------- 설정, 기록 ----------

def test_live_example_config_is_valid_and_live_checks_secrets(tmp_path):
    cfg = load_config(ROOT / "config.live-500.example.yaml")
    assert validate(cfg) == []
    errs = validate_live(cfg, load_secrets(tmp_path / "none.env"))
    assert any(".env" in e for e in errs)
    env = tmp_path / ".env"
    env.write_text("ONDO_API_KEY_ID=ondoKeyId_a\nONDO_API_SECRET=ondoApiSecret_b\n"
                   "LIGHTER_API_KEY_PRIVATE_KEY=0xabc\nLIGHTER_ACCOUNT_INDEX=1\nLIGHTER_API_KEY_INDEX=4\n")
    assert validate_live(cfg, load_secrets(env)) == []


def test_live_rejects_mixed_environments():
    cfg = load_config(ROOT / "config.example.yaml")  # 샌드박스/테스트넷 주문 + 메인넷 시세
    cfg.mode = "live"
    errs = validate_live(cfg, load_secrets(None))
    assert any("ondo.rest_url" in e for e in errs)
    assert any("lighter.base_url" in e for e in errs)


def test_ledger_dedupes_fills_and_report_runs():
    led = Ledger(":memory:")
    fill = {"id": "f1", "side": "sell", "price": "100.2", "size": "0.6", "fee": "0.006", "isMaker": True}
    assert led.record_fill("SOL", fill, D("100"), D("100.1")) is True
    assert led.record_fill("SOL", fill, D("100"), D("100.1")) is False
    now = time.time()
    led.record_quote(now, "SOL", "buy", D("99.82"), D("0.6"), D("100"), 25, 30)
    led.record_quote(now, "SOL", "sell", D("100.30"), D("0.6"), D("100"), 25, 30)  # 30bps: band 밖
    led.record_equity(D("250"), D("250"))
    led.record_equity(D("251"), D("249.5"))
    led.add_reward("2026-10-27", "2026-11-02", "1.5", "100")
    text = build_report(led, now - 60)
    assert "1건 (maker 1)" in text
    assert "+20.0bps" in text  # 매도 100.2 vs 중간가 100
    assert "변화 +0.50" in text
    assert "0.5 USD-시간" in text or "0 USD-시간" in text


async def test_token_bucket_spacing():
    b = TokenBucket(rate_per_sec=20, capacity=1)
    t0 = time.monotonic()
    for _ in range(3):
        await b.acquire()
    assert time.monotonic() - t0 >= 0.09  # 첫 번째는 즉시, 나머지 둘은 0.05초씩
