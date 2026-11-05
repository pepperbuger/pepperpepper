import time
from decimal import Decimal as D

from olbot.ledger import Ledger
from olbot.lighter.ws import LighterPublicWS
from olbot.market import MarketBoard, parse_lighter_ticker, parse_ondo_top_of_book
from olbot.ondo.ws import OndoPublicWS

# 2026-10-27에 실제로 받은 메시지 형식
ONDO_UPDATE = {
    "type": "update",
    "channel": "topOfBooksPerps",
    "timestamp": "2026-10-27T10:36:53.484414492Z",
    "data": [{
        "market": "LIT-USD.P",
        "time": "2026-10-27T10:36:53.092413835Z",
        "asks": [["2.984", "2923"]],
        "bids": [["2.9776", "3154"]],
    }],
}
ONDO_SUBSCRIBED = {
    "type": "subscribed",
    "channel": "topOfBooksPerps",
    "data": {"op": "subscribe", "channel": "topOfBooksPerps", "markets": ["LIT-USD.P"]},
}
LIGHTER_TICKER = {
    "channel": "ticker:1",
    "nonce": 25846801993,
    "ticker": {"s": "BTC", "a": {"price": "79621.4", "size": "0.002"}, "b": {"price": "79621.3", "size": "0.14"}},
    "type": "update/ticker",
}


def test_parse_ondo_update():
    assert parse_ondo_top_of_book(ONDO_UPDATE) == [("LIT", D("2.9776"), D("2.984"))]


def test_parse_ondo_ignores_non_updates_and_empty_sides():
    assert parse_ondo_top_of_book(ONDO_SUBSCRIBED) == []
    one_sided = {**ONDO_UPDATE, "data": [{**ONDO_UPDATE["data"][0], "asks": []}]}
    assert parse_ondo_top_of_book(one_sided) == []


def test_parse_lighter_ticker():
    assert parse_lighter_ticker(LIGHTER_TICKER) == (1, D("79621.3"), D("79621.4"))
    assert parse_lighter_ticker({**LIGHTER_TICKER, "type": "subscribed/ticker"}) is not None
    assert parse_lighter_ticker({"type": "pong"}) is None
    assert parse_lighter_ticker({"type": "update/ticker", "channel": "ticker:x"}) is None


def test_basis_sign_and_size():
    board = MarketBoard(["BTC"])
    now = time.time()
    board.update_ondo("BTC", D("99.9"), D("100.1"), now)  # mid 100
    assert board.states["BTC"].basis_bps() is None
    board.update_lighter("BTC", D("100.09"), D("100.11"), now)  # mid 100.1
    assert round(board.states["BTC"].basis_bps(), 6) == 10.0  # Lighter가 10bps 비쌈


def test_board_rejects_crossed_or_unknown_quotes():
    board = MarketBoard(["BTC"])
    board.update_ondo("BTC", D("101"), D("100"), time.time())
    board.update_ondo("ETH", D("1"), D("2"), time.time())
    assert board.states["BTC"].ondo is None
    assert "ETH" not in board.states


def test_ws_clients_route_messages_to_board():
    board = MarketBoard(["LIT", "BTC"])
    ondo = OndoPublicWS("wss://x", board, 30)
    lighter = LighterPublicWS("wss://y", board, {"BTC": 1, "LIT": 99}, 60)
    assert ondo.subscriptions()[0]["markets"] == ["LIT-USD.P", "BTC-USD.P"]
    assert {s["channel"] for s in lighter.subscriptions()} == {"ticker/1", "ticker/99"}
    now = time.time()
    ondo.on_message(ONDO_UPDATE, now)
    lighter.on_message(LIGHTER_TICKER, now)
    assert board.states["LIT"].ondo.mid == (D("2.9776") + D("2.984")) / 2
    assert board.states["BTC"].lighter.bid == D("79621.3")


def test_ledger_summary_and_gap():
    led = Ledger(":memory:")
    board = MarketBoard(["BTC"])
    t0 = time.time()
    board.update_ondo("BTC", D("99.9"), D("100.1"), t0)
    board.update_lighter("BTC", D("100.09"), D("100.11"), t0)
    for dt in (0, 10, 40):
        led.record_sample(board.states["BTC"], t0 + dt)
    led.record_event("ondo_ws", "disconnected")
    rows = led.basis_summary(t0 - 1)
    assert rows[0][0] == "BTC" and rows[0][1] == 3 and rows[0][2] == 3
    assert round(rows[0][3], 6) == 10.0
    assert led.max_sample_gap("BTC", t0 - 1) == 30
    assert led.event_counts(t0 - 1) == [("ondo_ws", "disconnected", 1)]


def test_basis_tracker_median_and_window():
    from olbot.market import BasisTracker

    tr = BasisTracker(window_sec=60)
    for i, b in enumerate([10, 12, 100, 11]):
        tr.add(1000 + i, b)
    assert tr.median(1003) == 11.5
    assert tr.median(1000 + 70) is None  # 창 밖으로 다 밀려남


def test_basis_status_uses_deviation_not_level():
    from olbot.market import basis_status

    # 평소 괴리가 31bps인 LIT 같은 마켓은 정상이어야 한다
    assert basis_status(31.0, 30.0, 15, 100) == "ok"
    assert basis_status(50.0, 30.0, 15, 100) == "deviation"
    assert basis_status(477.0, 470.0, 15, 100) == "sanity"  # OPENAI처럼 다른 자산
    assert basis_status(None, 30.0, 15, 100) == "missing"
    assert basis_status(31.0, None, 15, 100) == "ok"  # 아직 기록이 없으면 수준만 본다
