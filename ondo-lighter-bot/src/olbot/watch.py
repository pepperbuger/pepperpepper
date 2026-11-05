"""2단계: 시세 수신과 괴리 기록 (주문 없음, API 키 불필요).

Ondo `topOfBooksPerps`와 Lighter `ticker`를 받아 마켓별 괴리를 basis_sample_sec마다 SQLite에 남긴다.
끝나면(시간 만료 또는 Ctrl+C) 마켓별 통계와 연결 끊김 횟수를 출력한다.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import statistics
import time

from olbot.config import Config
from olbot.ledger import Ledger
from olbot.lighter.ws import LighterPublicWS
from olbot.market import BasisTracker, MarketBoard, basis_status
from olbot.markets import resolve
from olbot.ondo.ws import OndoPublicWS

log = logging.getLogger("olbot.watch")


def _fmt(x, nd=2) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def format_board(board: MarketBoard, trackers: dict[str, BasisTracker], cfg: Config, now: float) -> str:
    q = cfg.quoting
    lines = [
        f"{'base':6}{'ondo_bid':>12}{'ondo_ask':>12}{'lt_bid':>12}{'lt_ask':>12}"
        f"{'basis':>8}{'median':>8}{'o_age':>7}{'l_age':>7}  status"
    ]
    for base, st in board.states.items():
        o, l = st.ondo, st.lighter
        bps = st.basis_bps()
        med = trackers[base].median(now)
        lines.append(
            f"{base:6}"
            f"{str(o.bid) if o else '-':>12}{str(o.ask) if o else '-':>12}"
            f"{str(l.bid) if l else '-':>12}{str(l.ask) if l else '-':>12}"
            f"{_fmt(bps, 1):>8}{_fmt(med, 1):>8}"
            f"{_fmt(o.age(now) if o else None, 0):>7}{_fmt(l.age(now) if l else None, 0):>7}"
            f"  {basis_status(bps, med, q.basis_deviation_bps, q.basis_sanity_bps)}"
        )
    return "\n".join(lines)


async def run_watch(cfg: Config, bases: list[str], duration: float | None, report_every: float) -> int:
    md = cfg.market_data
    resolved = await resolve(bases, md.ondo_rest_url, md.lighter_rest_url)
    for p in resolved.problems:
        log.error(p)
    if resolved.problems:
        return 2

    board = MarketBoard(bases)
    trackers = {b: BasisTracker(cfg.quoting.basis_window_sec) for b in bases}
    ledger = Ledger(cfg.storage.sqlite_path)
    started = time.time()
    ledger.record_event("watch", "start", ",".join(bases))
    ondo_ws = OndoPublicWS(md.ondo_ws_url, board, md.ondo_ping_sec, ledger.record_event)
    lighter_ws = LighterPublicWS(md.lighter_ws_url, board, resolved.lighter_ids, md.lighter_ping_sec, ledger.record_event)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    async def sampler() -> None:
        last_report = 0.0
        while not stop.is_set():
            now = time.time()
            for st in board.states.values():
                ledger.record_sample(st, now)
                bps = st.basis_bps()
                if bps is not None:
                    trackers[st.base].add(now, bps)
            ledger.commit()
            if now - last_report >= report_every:
                last_report = now
                log.info(
                    "ondo_ws %s (silent %.0fs, reconnects %d) | lighter_ws %s (silent %.0fs, reconnects %d)\n%s",
                    "up" if ondo_ws.connected else "DOWN", ondo_ws.stale_for(now), ondo_ws.disconnect_count,
                    "up" if lighter_ws.connected else "DOWN", lighter_ws.stale_for(now), lighter_ws.disconnect_count,
                    format_board(board, trackers, cfg, now),
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.storage.basis_sample_sec)
            except asyncio.TimeoutError:
                pass

    tasks = [asyncio.create_task(ondo_ws.run()), asyncio.create_task(lighter_ws.run()), asyncio.create_task(sampler())]
    try:
        if duration:
            try:
                await asyncio.wait_for(stop.wait(), timeout=duration)
            except asyncio.TimeoutError:
                pass
        else:
            await stop.wait()
    finally:
        stop.set()
        ondo_ws.stop()
        lighter_ws.stop()
        await asyncio.wait(tasks, timeout=10)
        for t in tasks:
            t.cancel()
        ledger.record_event("watch", "stop", "")
        print(summary(ledger, bases, started))
        ledger.close()
    return 0


def summary(ledger: Ledger, bases: list[str], since: float) -> str:
    """dev_p95: 괴리가 전체 중앙값에서 벗어난 폭의 95번째 백분위. basis_deviation_bps를 정할 때 쓴다."""
    elapsed = time.time() - since
    lines = [f"\n=== watch 요약: {elapsed / 60:.1f}분 ===",
             f"{'base':6}{'samples':>9}{'both':>7}{'median':>8}{'min':>8}{'max':>8}{'stdev':>7}{'dev_p95':>9}{'max_gap_s':>11}"]
    for base, n, n_both, _avg, mn, mx in ledger.basis_summary(since):
        vals = ledger.basis_values(base, since)
        med = statistics.median(vals) if vals else None
        sd = statistics.pstdev(vals) if len(vals) > 1 else None
        devs = sorted(abs(v - med) for v in vals) if vals else []
        p95 = devs[min(len(devs) - 1, int(len(devs) * 0.95))] if devs else None
        lines.append(
            f"{base:6}{n:>9}{n_both:>7}{_fmt(med, 1):>8}{_fmt(mn, 1):>8}{_fmt(mx, 1):>8}"
            f"{_fmt(sd, 1):>7}{_fmt(p95, 1):>9}{ledger.max_sample_gap(base, since):>11.0f}"
        )
    lines.append("이벤트:")
    for source, kind, cnt in ledger.event_counts(since):
        lines.append(f"  {source:12}{kind:14}{cnt}")
    return "\n".join(lines)
