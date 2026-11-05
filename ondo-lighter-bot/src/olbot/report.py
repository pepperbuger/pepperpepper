"""운영 결과 리포트: 호가 노출, 체결, 헷지 비용, 자산 변화, 리워드.

핵심 지표 두 개:
- 1,000 USD-시간당 리워드: 받은 USDC / (band 안에 걸어둔 호가 USD x 시간 / 1000)
- 체결 1건당 헷지 비용(bps): Ondo 체결가와 Lighter 헷지 시점 중간가의 차이에서 평소 괴리를 뺀 값의 근사
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
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

    lines.append("\n[호가 노출] 계산호가 샘플의 band 내 USD-시간 (거래소 open·리워드 자격 보장 아님)")
    rows = db.execute(
        """SELECT base, price, size, in_band, interval_s, dist_bps
           FROM quote_snapshots WHERE ts >= ? ORDER BY base""",
        (since,),
    ).fetchall()
    exposure = {}
    for base, price, size, band, interval, distance in rows:
        a = exposure.setdefault(base, [Decimal(0), Decimal(0), Decimal(0), 0])
        hours_d = Decimal(str(interval)) / 3600
        a[0] += (_d(price) or Decimal(0)) * (_d(size) or Decimal(0)) * int(band) * hours_d
        a[1] += hours_d
        a[2] += _d(distance) or Decimal(0)
        a[3] += 1
    total_usd_hours = Decimal(0)
    for base, (usd_h, side_hours, dist_sum, n) in exposure.items():
        total_usd_hours += usd_h
        lines.append(f"  {base:6} {usd_h:>12,.2f} USD-시간   평균 거리 {dist_sum/n:.1f}bps   (양쪽 합산 {side_hours:.1f}시간)")
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
        "SELECT base, filled, error FROM hedges WHERE ts >= ?",
        (since,),
    ).fetchall()
    hedges = {}
    for base, filled, error in hrows:
        a = hedges.setdefault(base, {"sent": 0, "rejected": 0, "filled": 0, "other_final": 0})
        if (error or "").startswith("final:"):
            a["filled" if error == "final:filled" else "other_final"] += 1
        else:
            a["sent"] += 1
            a["rejected"] += int(filled != "pending" and bool(error))
    for base, a in sorted(hedges.items()):
        lines.append(f"  {base:6} 전송 시도 {a['sent']}건, 즉시 거절 {a['rejected']}건, "
                     f"최종 전량 체결 {a['filled']}건, 기타 최종 {a['other_final']}건")
    pending = db.execute("SELECT COUNT(*) FROM pending_hedges").fetchone()[0]
    lines.append(f"  현재 DB 미확정 헷지 {pending}건 (모든 계정 범위 합계)")
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
    for ws, we, usdc, pts, note in rw:
        try:
            start = datetime.combine(date.fromisoformat(ws), datetime.min.time(), timezone.utc).timestamp()
            end = datetime.combine(date.fromisoformat(we) + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp()
        except ValueError:
            lines.append(f"  {ws}~{we}: 주간 날짜 오류, 정규화 제외")
            continue
        if end <= since or start > time.time():
            continue
        lines.append(f"  {ws}~{we}: {usdc} USDC, {pts} 포인트 {note or ''}")
        if since > start or time.time() < end:
            lines.append("    조회 기간에 주간 전체가 포함되지 않아 리워드 정규화 제외")
            continue
        # 같은 리워드 주간의 노출만 사용한다. 다른 주의 리워드를 현재 노출로 나누지 않는다.
        week_rows = db.execute(
            "SELECT price,size,in_band,interval_s FROM quote_snapshots WHERE ts>=? AND ts<?", (start, end)
        ).fetchall()
        usd_h = sum(((_d(p) or Decimal(0)) * (_d(s) or Decimal(0)) * int(b) * Decimal(str(i)) / 3600
                     for p, s, b, i in week_rows), Decimal(0))
        if usd_h > 0:
            value = (_d(usdc) or Decimal(0)) * 1000 / usd_h
            lines.append(f"    같은 주 추정 노출 기준 1,000 USD-시간당 {value:.4f} USDC (입력 보상 종류·대상 확인 필요)")
    if not rw:
        lines.append("  입력 없음 — 미지급·미확인 보상을 수익 0으로 확정하지 않음")

    lines.append("\n[이벤트]")
    for source, kind, cnt in ledger.event_counts(since):
        lines.append(f"  {source:16}{kind:14}{cnt}")
    return "\n".join(lines)
