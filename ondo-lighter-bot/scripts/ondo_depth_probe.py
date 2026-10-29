#!/usr/bin/env python3
"""Ondo Perps 호가 두께 측정기 (읽기 전용, API 키 불필요).

Ondo와 Lighter에 둘 다 상장된 퍼프 마켓마다
  - Ondo 중간가 기준 +-band_bps 안에 걸린 호가 물량(USD)
  - Ondo 스프레드(bps)
  - Lighter 최근 체결가와 Ondo 중간가의 괴리(bps, 같은 기초자산인지 확인용)
  - Lighter 24시간 거래량(헷지 유동성 참고용)
을 뽑는다.

주의: 이 구형 측정기는 호가 두께만 비교하며 유동성 보상 자격을 판정하지 않는다.
2026-10-27 공식 주간 목록에 LIT/ENA는 없고 거리별 배분도 다르다.
최신 근거는 docs/REWARD_WEEK_PLAN.md, Decimal 기반 관측은 olbot.reward_probe를 쓴다.

사용법:
  python3 ondo_depth_probe.py                 # 겹치는 마켓 전체
  python3 ondo_depth_probe.py --band 25 --csv out.csv
  python3 ondo_depth_probe.py --markets BTC,ETH,SOL

표준 라이브러리만 사용한다.
"""

import argparse
import csv
import json
import sys
import time
import urllib.request

ONDO = "https://api.ondoperps.xyz"
LIGHTER = "https://mainnet.zklighter.elliot.ai"
# Ondo 공개 엔드포인트도 과하게 두드리지 않도록 요청 사이 간격을 둔다.
REQUEST_GAP_SEC = 1.1


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ondo-depth-probe/0.1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def ondo_perp_markets():
    data = get_json(f"{ONDO}/v1/markets")["result"]["perps"]["tradingPairs"]
    return {m["pair"]["base"].upper(): m for m in data if not m.get("disabled")}


def lighter_perps():
    books = get_json(f"{LIGHTER}/api/v1/orderBooks")["order_books"]
    active = {
        b["symbol"].upper()
        for b in books
        if b.get("status") == "active" and b.get("market_type") == "perp"
    }
    stats = get_json(f"{LIGHTER}/api/v1/exchangeStats")["order_book_stats"]
    by_symbol = {s["symbol"].upper(): s for s in stats}
    return {sym: by_symbol.get(sym, {}) for sym in active}


def band_depth(levels, mid, band_bps, side):
    """mid에서 band_bps 안에 있는 호가의 USD 합계와, 반환된 레벨이 band 끝까지 닿았는지."""
    limit = mid * (1 - band_bps / 1e4) if side == "bid" else mid * (1 + band_bps / 1e4)
    total = 0.0
    last_price = None
    for price_s, size_s in levels:
        price, size = float(price_s), float(size_s)
        last_price = price
        inside = price >= limit if side == "bid" else price <= limit
        if not inside:
            return total, True
        total += price * size
    # 100레벨을 다 써도 band 끝에 못 닿았으면 실제 물량은 이보다 크다.
    reached = last_price is not None and (
        last_price <= limit if side == "bid" else last_price >= limit
    )
    return total, reached


def probe(market, band_bps):
    book = get_json(f"{ONDO}/v1/perps/depth?market={market}&depth=100")["result"]
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        return None
    best_bid, best_ask = float(bids[0][0]), float(asks[0][0])
    mid = (best_bid + best_ask) / 2
    bid_usd, bid_full = band_depth(bids, mid, band_bps, "bid")
    ask_usd, ask_full = band_depth(asks, mid, band_bps, "ask")
    return {
        "mid": mid,
        "spread_bps": (best_ask - best_bid) / mid * 1e4,
        "bid_usd": bid_usd,
        "ask_usd": ask_usd,
        "complete": bid_full and ask_full,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--band", type=float, default=25.0, help="중간가 기준 band (bps)")
    ap.add_argument("--markets", help="쉼표로 구분한 기초자산 목록 (예: BTC,ETH)")
    ap.add_argument("--csv", help="결과를 저장할 CSV 경로")
    args = ap.parse_args()

    ondo = ondo_perp_markets()
    lighter = lighter_perps()
    overlap = sorted(set(ondo) & set(lighter))
    if args.markets:
        wanted = {m.strip().upper() for m in args.markets.split(",")}
        overlap = [b for b in overlap if b in wanted]
    print(f"Ondo 활성 퍼프 {len(ondo)}개, Lighter 활성 퍼프 {len(lighter)}개, 겹침 {len(overlap)}개", file=sys.stderr)
    print(f"Ondo 전용(헷지 불가): {', '.join(sorted(set(ondo) - set(lighter)))}", file=sys.stderr)

    rows = []
    for base in overlap:
        market = ondo[base]["market"]
        try:
            r = probe(market, args.band)
        except Exception as e:  # 개별 마켓 실패는 건너뛴다
            print(f"{market}: {e}", file=sys.stderr)
            r = None
        time.sleep(REQUEST_GAP_SEC)
        if not r:
            continue
        lt = lighter[base]
        l_last = float(lt.get("last_trade_price") or 0)
        gap_bps = (l_last / r["mid"] - 1) * 1e4 if l_last else None
        rows.append({
            "base": base,
            "ondo_market": market,
            "tags": "/".join(ondo[base].get("tags") or []),
            "mid": round(r["mid"], 6),
            "ondo_spread_bps": round(r["spread_bps"], 2),
            "band_bid_usd": round(r["bid_usd"]),
            "band_ask_usd": round(r["ask_usd"]),
            "band_total_usd": round(r["bid_usd"] + r["ask_usd"]),
            "band_complete": r["complete"],
            "lighter_gap_bps": round(gap_bps, 1) if gap_bps is not None else "",
            "lighter_vol24h_usd": round(float(lt.get("daily_quote_token_volume") or 0)),
        })

    rows.sort(key=lambda x: x["band_total_usd"])
    hdr = f"{'base':8}{'tags':10}{'spread':>8}{'band_bid$':>12}{'band_ask$':>12}{'L_gap':>8}{'L_vol24h$':>14}"
    print(hdr)
    for x in rows:
        mark = "" if x["band_complete"] else "+"  # +: 100레벨로 band 끝까지 못 봄(실제는 더 두꺼움)
        print(f"{x['base']:8}{x['tags'][:9]:10}{x['ondo_spread_bps']:8.1f}"
              f"{x['band_bid_usd']:>11,}{mark:1}{x['band_ask_usd']:>11,}{mark:1}"
              f"{str(x['lighter_gap_bps']):>8}{x['lighter_vol24h_usd']:>14,}")
    if args.csv and rows:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"saved {args.csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
