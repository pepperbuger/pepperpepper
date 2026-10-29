from datetime import datetime, timezone
from decimal import Decimal as D

from olbot.ledger import Ledger
from olbot.report import build_report


def test_final_hedge_rows_are_neither_duplicate_sends_nor_rejections(monkeypatch):
    monkeypatch.setattr('olbot.report.time.time', lambda: 1000)
    with_ledger = Ledger(':memory:')
    try:
        for filled, error in [('pending', ''), ('20', 'final:filled'),
                              ('0', 'HTTP400'), ('pending', 'lost'), ('3', 'final:canceled')]:
            with_ledger.db.execute('INSERT INTO hedges VALUES (?,?,?,?,?,?,?,?)',
                                   (950, 'LIT', '20', 'sell', '3', '3', filled, error))
        report = build_report(with_ledger, 900)
        assert '전송 시도 3건, 즉시 거절 1건, 최종 전량 체결 1건, 기타 최종 1건' in report
        assert '미지급·미확인 보상을 수익 0으로 확정하지 않음' in report
    finally:
        with_ledger.close()


def test_reward_normalization_uses_same_full_week_only(monkeypatch):
    start = datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr('olbot.report.time.time', lambda: start + 14 * 86400)
    ledger = Ledger(':memory:')
    try:
        for ts, size in [(start + 100, '10'), (start + 8 * 86400, '999')]:
            ledger.record_quote(ts, 'ETH', 'buy', D('100'), D(size), D('100'), 25, 3600)
        ledger.db.execute('INSERT INTO rewards VALUES (?,?,?,?,?,?)',
                          (start + 8 * 86400, '2026-10-20', '2026-10-26', '2', '0', 'liquidity'))
        report = build_report(ledger, start)
        assert '같은 주 추정 노출 기준 1,000 USD-시간당 2.0000 USDC' in report
        short = build_report(ledger, start + 200)
        assert '주간 전체가 포함되지 않아 리워드 정규화 제외' in short
        assert '1,000 USD-시간당 2.0000' not in short
    finally:
        ledger.close()
