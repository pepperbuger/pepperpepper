"""운영 결과 리포트: 호가 노출, 체결, 헷지 비용, 자산 변화, 리워드.

핵심 지표 두 개:
- 1,000 USD-시간당 리워드: 받은 USDC / (band 안에 걸어둔 호가 USD x 시간 / 1000)
- 체결 1건당 헷지 비용(bps): Ondo 체결가와 Lighter 헷지 시점 중간가의 차이에서 평소 괴리를 뺀 값의 근사
"""

from __future__ import annotations

import time
from decimal import Decimal

from olbot.ledger import Ledger


def _d(x) -> Decimal | None:
    try:
        return None if x in (None, "None", "") else Decimal(str(x))
    except Exception:
        return None


def build_report(ledger: Ledger, since: float) -> str:
    db = ledger.db
    hours = (time.time() - since) / 3600
    lines = [f"=== 운영 리포트: 최근 {hours:.1f}시간 ==="]

    lines.append("\n[호가 노출] band 안에 걸려 있던 호가 (USD-시간)")
    rows = db.execute(
        """SELECT base, SUM(notional_usd * in_band * interval_s) / 3600.0, SUM(interval_s) / 3600.0, AVG(dist_bps)
           FROM quote_snapshots WHERE ts >= ? GROUP BY base ORDER BY base""",
        (since,),
    ).fetchall()
    total_usd_hours = 0.0
    for base, usd_h, side_hours, avg_dist in rows:
        total_usd_hours += usd_h or 0
        lines.append(f"  {base:6} {usd_h or 0:>12,.0f} USD-시간   평균 거리 {avg_dist or 0:.1f}bps   (양쪽 합산 {side_hours:.1f}시간)")
    lines.append(f"  합계   {total_usd_hours:>12,.0f} USD-시간")

    lines.append("\n[체결] Ondo")
    fills = db.execute(
        "SELECT base, side, price, size, fee, is_maker, ondo_mid, lighter_mid FROM fills WHERE ts >= ?", (since,)
    ).fetchall()
    by_base: dict[str, dict] = {}
    for base, side, price, size, fee, is_maker, ondo_mid, _lmid in fills:
        p, s, f, om = _d(price), _d(size), _d(fee) or Decimal(0), _d(ondo_mid)
        agg = by_base.setdefault(base, {"n": 0, "notional": Decimal(0), "fee": Decimal(0), "maker": 0, "edge": []})
        agg["n"] += 1
        agg["maker"] += int(is_maker or 0)
        if p is not None and s is not None:
            agg["notional"] += p * s
        agg["fee"] += f
        if p is not None and om:
            # 체결가가 중간가보다 유리한 정도 (maker면 보통 +)
            sign = 1 if side == "sell" else -1
            agg["edge"].append(float(sign * (p - om) / om * 10_000))
    for base, a in sorted(by_base.items()):
        edge = sum(a["edge"]) / len(a["edge"]) if a["edge"] else None
        lines.append(
            f"  {base:6} {a['n']}건 (maker {a['maker']})  거래량 {a['notional']:,.2f} USD  수수료 {a['fee']:.4f} USD"
            + (f"  중간가 대비 체결 우위 {edge:+.1f}bps" if edge is not None else "")
        )
    if not by_base:
        lines.append("  체결 없음")

    lines.append("\n[헷지] Lighter")
    hrows = db.execute(
        "SELECT base, COUNT(*), SUM(CASE WHEN error != '' THEN 1 ELSE 0 END) FROM hedges WHERE ts >= ? GROUP BY base",
        (since,),
    ).fetchall()
    for base, n, errs in hrows:
        lines.append(f"  {base:6} 헷지 주문 {n}건, 거절 {errs}건")
    if not hrows:
        lines.append("  헷지 없음")

    lines.append("\n[자산] Ondo + Lighter (USD)")
    eq = db.execute("SELECT ts, total_usd FROM equity WHERE ts >= ? AND total_usd != 'None' ORDER BY ts", (since,)).fetchall()
    if eq:
        start, end = _d(eq[0][1]), _d(eq[-1][1])
        lines.append(f"  시작 {start:.2f} -> 현재 {end:.2f}  변화 {end - start:+.2f}")
    else:
        lines.append("  기록 없음 (dry_run이면 정상)")

    lines.append("\n[리워드] 수동 입력분 (python -m olbot report add-reward ...)")
    rw = db.execute("SELECT week_start, week_end, usdc, points, note FROM rewards ORDER BY ts").fetchall()
    total_usdc = Decimal(0)
    for ws, we, usdc, pts, note in rw:
        total_usdc += _d(usdc) or Decimal(0)
        lines.append(f"  {ws}~{we}: {usdc} USDC, {pts} 포인트 {note or ''}")
    if not rw:
        lines.append("  입력 없음")
    elif total_usd_hours > 0:
        lines.append(f"  1,000 USD-시간당 리워드(조회 기간 노출 기준 근사): {float(total_usdc) / (total_usd_hours / 1000):.4f} USDC")

    lines.append("\n[이벤트]")
    for source, kind, cnt in ledger.event_counts(since):
        lines.append(f"  {source:16}{kind:14}{cnt}")
    return "\n".join(lines)
