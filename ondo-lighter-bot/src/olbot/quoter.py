"""호가 계산과 재호가 판단 (순수 함수, 거래소 호출 없음).

매수 호가 = 공정가 x (1 - offset), 매도 호가 = 공정가 x (1 + offset).
재고가 롱이면 두 호가를 아래로, 숏이면 위로 민다(skew). 재고 한도에 닿으면 재고를 늘리는 쪽은 걸지 않는다.
매수는 호가 단위로 내림, 매도는 올림해서 band 안쪽으로 너무 붙지 않게 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

BPS = Decimal(10_000)
SIZE_TOLERANCE = Decimal("0.2")


@dataclass(frozen=True)
class QuoteSide:
    price: Decimal
    size: Decimal

    def notional(self) -> Decimal:
        return self.price * self.size


@dataclass(frozen=True)
class TargetQuotes:
    bid: QuoteSide | None
    ask: QuoteSide | None


def round_to(value: Decimal, increment: Decimal, rounding: str) -> Decimal:
    return (value / increment).to_integral_value(rounding=rounding) * increment


def target_quotes(
    fair: Decimal,
    offset_bps: float,
    size_usd: Decimal,
    inventory_base: Decimal,
    max_inventory_usd: Decimal,
    skew_bps_per_1k: float,
    price_increment: Decimal,
    size_increment: Decimal,
    min_size: Decimal = Decimal(0),
) -> TargetQuotes:
    inventory_usd = inventory_base * fair
    skew = Decimal(str(skew_bps_per_1k)) * inventory_usd / 1000  # bps, 롱이면 양수 -> 호가를 아래로
    off = Decimal(str(offset_bps))
    bid_px = round_to(fair * (1 - (off + skew) / BPS), price_increment, ROUND_FLOOR)
    ask_px = round_to(fair * (1 + (off - skew) / BPS), price_increment, ROUND_CEILING)
    size = round_to(size_usd / fair, size_increment, ROUND_FLOOR)
    if size <= 0 or size < min_size or bid_px <= 0 or ask_px <= bid_px:
        return TargetQuotes(None, None)
    bid = QuoteSide(bid_px, size) if inventory_usd < max_inventory_usd else None
    ask = QuoteSide(ask_px, size) if inventory_usd > -max_inventory_usd else None
    return TargetQuotes(bid, ask)


def distance_bps(price: Decimal, mid: Decimal) -> float:
    return float(abs(price - mid) / mid * BPS)


def needs_requote(
    current: TargetQuotes | None,
    target: TargetQuotes,
    mid: Decimal,
    band_bps: float,
    threshold_bps: float,
) -> bool:
    if current is None:
        return target.bid is not None or target.ask is not None
    for cur, tgt in ((current.bid, target.bid), (current.ask, target.ask)):
        if (cur is None) != (tgt is None):
            return True
        if cur is None:
            continue
        if distance_bps(cur.price, mid) > band_bps:
            return True
        if float(abs(cur.price - tgt.price) / mid * BPS) > threshold_bps:
            return True
        # 수량은 가격이 조금만 움직여도 단위 하나씩 바뀐다. 요청 예산을 아끼려고 20% 넘게 다를 때만 고친다.
        if abs(cur.size - tgt.size) > tgt.size * SIZE_TOLERANCE:
            return True
    return False


def to_order_payloads(market: str, q: TargetQuotes) -> list[dict]:
    orders = []
    for side, s in (("buy", q.bid), ("sell", q.ask)):
        if s is not None:
            orders.append({
                "market": market,
                "side": side,
                "type": "limit",
                "timeInForce": "GTC",
                "postOnly": True,
                "price": format(s.price, "f"),
                "size": format(s.size, "f"),
            })
    return orders
