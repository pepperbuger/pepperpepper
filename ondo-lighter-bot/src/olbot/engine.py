"""실운영 엔진: 호가 + 헷지 + 안전장치 + 기록.

dry_run: 키 없이 공개 시세만 받아 낼 호가를 계산하고 기록한다. 주문은 보내지 않는다.
live: Ondo에 post-only 호가를 걸고, 체결되면 Lighter에서 헷지한다.

시작 순서(live): Lighter 키 확인 -> Ondo 주문 전부 취소 -> 레버리지 설정 -> 양쪽 포지션 대사와 차이 헷지
-> 비공개 WS 로그인(데드맨 스위치 켜짐) -> 호가 시작.
종료(Ctrl+C): Ondo 주문 전부 취소, 헷지된 포지션은 그대로 둔다.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from decimal import Decimal

from olbot.config import Config, Secrets
from olbot.hedger import Hedger
from olbot.ledger import Ledger, pending_scope
from olbot.lighter.ws import LighterPublicWS
from olbot.market import BasisTracker, MarketBoard, basis_status
from olbot.markets import fetch_ondo_perps, resolve
from olbot.ondo.ws import OndoPublicWS
from olbot.quoter import TargetQuotes, QuoteSide, needs_requote, target_quotes, to_order_payloads
from olbot.ratelimit import TokenBucket
from olbot.risk import RiskInputs, evaluate, pending_pause_reason

log = logging.getLogger("olbot.engine")

REQUOTE_COST = 2  # 마켓 단위 전체 취소 1회 + 일괄 생성 1회
FILL_SETTLE_SEC = 5.0
# 주문 상태를 모르는 마켓. None(확실히 없음)과 구분해서, 다음 루프가 취소부터 하게 만든다.
UNKNOWN_QUOTES = TargetQuotes(None, None)  # 체결 직후 이 시간 동안은 REST 포지션 스냅샷으로 덮어쓰지 않는다


class Engine:
    def __init__(self, cfg: Config, secrets: Secrets, live: bool, ledger: Ledger) -> None:
        self.cfg = cfg
        self.secrets = secrets
        self.live = live
        self.ledger = ledger
        self.bases = cfg.bases
        self.mcfg = {m.base: m for m in cfg.markets}
        self.board = MarketBoard(self.bases)
        self.trackers = {b: BasisTracker(cfg.quoting.basis_window_sec) for b in self.bases}
        self.bucket = TokenBucket(cfg.ondo.rest_rate_per_sec, capacity=5)
        self.current: dict[str, TargetQuotes | None] = {b: None for b in self.bases}
        self.dirty: set[str] = set()
        self.last_fill_ts: dict[str, float] = {}
        self.killed: str | None = None
        self.paused: str | None = None
        self.stop = asyncio.Event()
        self.ondo_meta: dict[str, dict] = {}
        self.lighter_markets = {}
        self.ondo_rest = None
        self.private_ws = None
        self.trader = None
        self.hedger: Hedger | None = None
        self.equity_start: Decimal | None = None
        self.equity_now: Decimal | None = None
        self.ondo_margin_usage: float | None = None
        self.ws_bad_since: float | None = None
        self.positions_ok_ts: float | None = None  # 마지막으로 양쪽 포지션 조회에 성공한 시각
        self.requests_sent = 0
        self.started = time.time()

    # ---------- 준비 ----------

    async def setup(self) -> None:
        md = self.cfg.market_data
        resolved = await resolve(self.bases, md.ondo_rest_url, md.lighter_rest_url)
        if resolved.problems:
            raise RuntimeError("; ".join(resolved.problems))
        self.ondo_meta = {b: m for b, m in (await fetch_ondo_perps(md.ondo_rest_url)).items() if b in self.bases}
        self.ondo_ws = OndoPublicWS(md.ondo_ws_url, self.board, md.ondo_ping_sec, self.ledger.record_event)
        self.lighter_ws = LighterPublicWS(md.lighter_ws_url, self.board, resolved.lighter_ids, md.lighter_ping_sec,
                                          self.ledger.record_event)

        from olbot.lighter.client import LighterTrader, fetch_market_meta

        self.lighter_markets = {
            b: await fetch_market_meta(md.lighter_rest_url, b, resolved.lighter_ids[b]) for b in self.bases
        }
        if not self.live:
            return

        from olbot.ondo.private_ws import OndoPrivateWS
        from olbot.ondo.rest import OndoRest

        s = self.secrets
        self.ondo_rest = OndoRest(self.cfg.ondo.rest_url, s.ondo_api_key_id, s.ondo_api_secret, self.bucket)
        self.trader = LighterTrader(self.cfg.lighter.base_url, int(s.lighter_account_index),
                                    int(s.lighter_api_key_index), s.lighter_api_key_private_key)
        scope = pending_scope(self.cfg.lighter.base_url, int(s.lighter_account_index))
        self.hedger = Hedger(
            self.trader, self.lighter_markets, self.cfg.hedging.max_slippage_bps, self.cfg.hedging.retries,
            record=self.ledger.record_hedge,
            initial_pending=self.ledger.load_pending_hedges(scope),
            save_pending=lambda base, pending: self.ledger.save_pending_hedge(scope, base, pending),
        )
        self.private_ws = OndoPrivateWS(
            self.cfg.ondo.ws_url, s.ondo_api_key_id, s.ondo_api_secret,
            [self.ondo_meta[b]["market"] for b in self.bases],
            self.cfg.ondo.deadman_timeout_sec, self.cfg.ondo.deadman_heartbeat_sec,
            self.on_fill, self.ledger.record_event,
        )

    async def live_startup(self) -> None:
        log.info("Lighter API 키 확인")
        await self.trader.connect()
        log.info("Ondo 주문 전부 취소")
        await self.ondo_rest.cancel_all()
        for b in self.bases:
            await self.ondo_rest.set_leverage(self.ondo_meta[b]["market"], self.cfg.ondo.leverage)
        await self.reconcile_positions()
        await self.update_equity()
        self.equity_start = self.equity_now
        log.info("시작 자산 %s USD (Ondo+Lighter)", self.equity_start)
        for b in self.bases:
            await self.hedger.hedge(b, self.lighter_mid(b))

    # ---------- 상태 ----------

    def ondo_mid(self, b: str) -> Decimal | None:
        q = self.board.states[b].ondo
        return q.mid if q else None

    def lighter_mid(self, b: str) -> Decimal | None:
        q = self.board.states[b].lighter
        return q.mid if q else None

    def market_block_reason(self, b: str, now: float) -> str | None:
        st = self.board.states[b]
        bps = st.basis_bps()
        dev = self.mcfg[b].basis_deviation_bps or self.cfg.quoting.basis_deviation_bps
        status = basis_status(bps, self.trackers[b].median(now), dev, self.cfg.quoting.basis_sanity_bps)
        return None if status == "ok" else status

    def min_hedge_usd(self, b: str, mid: Decimal) -> Decimal:
        return self.lighter_markets[b].min_hedge_size(mid) * mid

    def inventory(self, b: str) -> Decimal:
        return self.hedger.states[b].ondo_pos if self.hedger else Decimal(0)

    # ---------- 이벤트 ----------

    def on_fill(self, fill: dict) -> None:
        base = fill.get("market", "").split("-", 1)[0].upper()
        if base not in self.board.states:
            return
        is_new = self.ledger.record_fill(base, fill, self.ondo_mid(base), self.lighter_mid(base))
        if not is_new:
            return
        size = Decimal(fill.get("size") or "0")
        signed = size if fill.get("side") == "buy" else -size
        st = self.hedger.states[base]
        st.ondo_pos += signed  # 다음 REST 대사(FILL_SETTLE_SEC 이후)가 실제 값으로 덮어쓴다
        self.last_fill_ts[base] = time.time()
        self.dirty.add(base)
        log.info("FILL %s %s %s @ %s maker=%s", base, fill.get("side"), size, fill.get("price"), fill.get("isMaker"))
        task = asyncio.get_running_loop().create_task(self.hedger.hedge(base, self.lighter_mid(base)))
        task.add_done_callback(_log_task_error)

    async def reconcile_positions(self) -> None:
        """Ondo 포지션은 REST가 기준이다. WS 포지션 채널은 청산된 포지션을 0으로 보내준다는 보장이 없다."""
        by_market = await self.ondo_rest.positions()
        by_base = {m.split("-", 1)[0].upper(): q for m, q in by_market.items()}
        now = time.time()
        recent = {b for b, t in self.last_fill_ts.items() if now - t < FILL_SETTLE_SEC}
        self.hedger.set_ondo_positions(by_base, skip=recent)
        await self.hedger.refresh_lighter()
        self.positions_ok_ts = time.time()

    async def update_equity(self) -> None:
        bal = await self.ondo_rest.balance()
        ondo_usd = Decimal(bal.get("marginBalance") or "0")
        used = Decimal(bal.get("usedMargin") or "0")
        self.ondo_margin_usage = float(used / ondo_usd) if ondo_usd > 0 else None
        acct = await self.trader.account()
        lighter_usd = Decimal(str(acct.get("total_asset_value") or "0"))
        self.equity_now = ondo_usd + lighter_usd
        self.ledger.record_equity(ondo_usd, lighter_usd)

    async def kill(self, reason: str) -> None:
        if self.killed:
            return
        self.killed = reason
        log.error("KILL: %s -> Ondo 호가 전부 취소, 신규 호가 중단 (헷지는 계속)", reason)
        self.ledger.record_event("risk", "kill", reason)
        # 실패해도 주문 추적이 남아 있으므로 quote_loop가 마켓별 취소를 계속 다시 시도한다
        await self.cancel_all_quotes()

    async def cancel_all_quotes(self, attempts: int = 1) -> bool:
        """Ondo 주문 전부 취소. 성공했을 때만 로컬 추적을 지운다 (실패하면 거래소에 남아 있을 수 있다)."""
        if not self.live or self.ondo_rest is None:
            for b in self.bases:
                self.current[b] = None
            return True
        for i in range(attempts):
            try:
                await self.ondo_rest.cancel_all()
                self.requests_sent += 1
                for b in self.bases:
                    self.current[b] = None
                return True
            except Exception as e:
                log.error("cancel_all 실패 (%d/%d): %r", i + 1, attempts, e)
                self.ledger.record_event("engine", "cancel_failed", repr(e)[:200])
                if i + 1 < attempts:
                    await asyncio.sleep(2 ** i)
        return False

    # ---------- 루프 ----------

    async def quote_loop(self) -> None:
        i = 0
        while not self.stop.is_set():
            await asyncio.sleep(1.0)
            if self.stop.is_set():
                return
            now = time.time()
            for st in self.board.states.values():
                bps = st.basis_bps()
                if bps is not None:
                    self.trackers[st.base].add(now, bps)
            if not self.bases:
                continue
            b = self.bases[i % len(self.bases)]
            i += 1
            try:
                await self._quote_market(b, now)
            except Exception:
                log.exception("%s 호가 루프 오류 (계속 진행)", b)

    async def _quote_market(self, b: str, now: float) -> None:
        q = self.cfg.quoting
        mid = self.ondo_mid(b)
        pending_block = pending_pause_reason(self.hedger.overdue_pending(now)) if self.hedger else None
        blocked = self.killed or self.paused or pending_block or (mid is None and "no_quote") or self.market_block_reason(b, now)
        if blocked:
            if self.current[b] is not None:
                await self.pull_market(b, str(blocked))
            return
        m = self.mcfg[b]
        meta = self.ondo_meta[b]
        total_inv = sum(abs(self.inventory(x) * (self.ondo_mid(x) or 0)) for x in self.bases)
        max_inv = m.max_inventory_usd
        if total_inv >= self.cfg.risk.max_total_inventory_usd:
            max_inv = min(max_inv, abs(self.inventory(b) * mid))
        target = target_quotes(
            mid, q.offset_bps, m.size_usd_per_side, self.inventory(b), max_inv, q.skew_bps_per_1k,
            Decimal(meta["quoteIncrement"]), Decimal(meta["baseIncrement"]),
            min_size=self.lighter_markets[b].min_hedge_size(mid),
        )
        unknown = self.current[b] is UNKNOWN_QUOTES
        if not unknown and b not in self.dirty and not needs_requote(
            self.current[b], target, mid, q.band_bps, q.requote_threshold_bps
        ):
            return
        if self.live and self.bucket.available() < REQUOTE_COST + 1:
            return
        await self.place(b, target)

    async def pull_market(self, b: str, reason: str) -> bool:
        """한 마켓의 호가를 내린다. 실패하면 추적을 남겨 다음 루프에서 다시 시도한다."""
        log.info("%s 호가 내림: %s", b, reason)
        self.requests_sent += 1
        if self.live:
            try:
                await self.ondo_rest.cancel_all(self.ondo_meta[b]["market"])
            except Exception as e:
                log.error("%s cancel 실패, 다음 루프에서 다시 시도: %r", b, e)
                self.ledger.record_event("engine", "cancel_failed", f"{b} {e!r}"[:200])
                return False
        self.current[b] = None
        return True

    async def place(self, b: str, target: TargetQuotes) -> None:
        market = self.ondo_meta[b]["market"]
        self.dirty.discard(b)
        self.requests_sent += 2
        if not self.live:
            self.current[b] = target
            return
        try:
            await self.ondo_rest.cancel_all(market)
            self.current[b] = None
            # 취소/요청 예산 대기 중에 미확정 주문이 오래됐을 수도 있다.
            if (self.killed or self.paused or self.stop.is_set()
                    or (self.hedger and self.hedger.overdue_pending(time.time()))):
                return
            payload = to_order_payloads(market, target)
            if not payload:
                return
            added, failed = await self.ondo_rest.create_batch(payload)
        except Exception as e:
            # 요청이 거래소에 닿았는지 모른다. 비어 있다고 보지 않고 "확인 필요"로 남겨
            # 다음 재호가나 정지 때 마켓 단위 취소부터 하게 한다.
            log.error("%s 호가 실패: %r", b, e)
            self.current[b] = UNKNOWN_QUOTES
            return
        bid = ask = None
        for o in added:
            s = QuoteSide(Decimal(o["price"]), Decimal(o["size"]))
            if o.get("side") == "buy":
                bid = s
            else:
                ask = s
        for f in failed:
            log.warning("%s 주문 거절: %s", b, f)
        self.current[b] = TargetQuotes(bid, ask)
        if (self.killed or self.paused or self.stop.is_set()
                or (self.hedger and self.hedger.overdue_pending(time.time()))):
            await self.pull_market(b, self.killed or self.paused or "stopping or unconfirmed hedge")

    async def hedge_loop(self) -> None:
        while not self.stop.is_set():
            await asyncio.sleep(self.cfg.hedging.reconcile_interval_sec)
            now = time.time()
            try:
                await self.reconcile_positions()
            except Exception as e:
                log.warning("포지션 대사 실패: %r", e)
                continue
            for b in self.bases:
                st = self.hedger.states[b]
                mid = self.lighter_mid(b) or self.ondo_mid(b)
                if mid is None:
                    continue
                if st.pending is not None or self.hedger.unhedged_usd(b, mid) >= self.min_hedge_usd(b, mid):
                    try:
                        await self.hedger.hedge(b, self.lighter_mid(b))
                    except Exception:
                        # Hedger.hedge는 예외를 밖으로 내지 않게 만들었지만, 만약을 위해 루프는 절대 죽지 않게 한다
                        log.exception("%s 헷지 오류 (다음 대사에서 다시 시도)", b)
                        st.consecutive_failures += 1

    async def risk_loop(self) -> None:
        while not self.stop.is_set():
            await asyncio.sleep(1.0)
            try:
                await self._risk_tick()
            except Exception:
                log.exception("위험 판단 오류 -> 안전을 위해 정지")
                await self.kill("위험 판단 루프 오류")

    async def _risk_tick(self) -> None:
        r = self.cfg.risk
        now = time.time()
        ws_ok = (self.ondo_ws.connected and self.lighter_ws.connected
                 and self.ondo_ws.stale_for(now) < 65 and self.lighter_ws.stale_for(now) < 65)
        self.ws_bad_since = None if ws_ok else (self.ws_bad_since or now)
        silent = 0.0 if self.ws_bad_since is None else now - self.ws_bad_since
        if not self.live:
            self.paused = f"시세 WS 이상 {silent:.0f}초" if silent > r.ws_stale_sec else None
            return
        mids = {b: self.lighter_mid(b) or self.ondo_mid(b) or Decimal(0) for b in self.bases}
        # 노출 타이머는 대사 성공 여부와 상관없이 로컬 상태(체결 즉시 반영)로 매초 갱신한다.
        # 포지션 조회가 계속 실패해도 체결로 생긴 노출은 여기서 잡혀 max_unhedged_sec 뒤 kill된다.
        for b in self.bases:
            st = self.hedger.states[b]
            if mids[b] > 0 and self.hedger.unhedged_usd(b, mids[b]) >= self.min_hedge_usd(b, mids[b]):
                st.unhedged_since = st.unhedged_since or now
            else:
                st.unhedged_since = None
        decision = evaluate(
            RiskInputs(
                now=now,
                unhedged_usd={b: self.hedger.unhedged_usd(b, mids[b]) for b in self.bases},
                unhedged_since={b: self.hedger.states[b].unhedged_since for b in self.bases},
                hedge_failures={b: self.hedger.states[b].consecutive_failures for b in self.bases},
                public_ws_silent_sec=silent,
                private_ws_up=self.private_ws.ready,
                equity_start=self.equity_start,
                equity_now=self.equity_now,
                ondo_margin_usage=self.ondo_margin_usage,
                positions_stale_sec=now - (self.positions_ok_ts or now),
                pending_hedges_overdue=self.hedger.overdue_pending(now),
            ),
            r.max_unhedged_usd, r.max_unhedged_sec, r.ws_stale_sec, r.daily_loss_limit_usd, r.max_margin_usage,
            r.positions_stale_sec,
        )
        if decision.kill_reason:
            await self.kill(decision.kill_reason)
        if decision.pause_reason != self.paused:
            if decision.pause_reason:
                log.warning("PAUSE: %s", decision.pause_reason)
                self.ledger.record_event("risk", "pause", decision.pause_reason)
            elif self.paused:
                log.info("RESUME (%s 해소)", self.paused)
                self.ledger.record_event("risk", "resume", self.paused)
            self.paused = decision.pause_reason

    async def record_loop(self) -> None:
        st = self.cfg.storage
        self._last = {"equity": 0.0, "basis": 0.0, "status": 0.0}
        while not self.stop.is_set():
            await asyncio.sleep(st.quote_snapshot_sec)
            try:
                await self._record_tick()
            except Exception:
                log.exception("기록 루프 오류 (계속 진행)")

    async def _record_tick(self) -> None:
        st = self.cfg.storage
        last = self._last
        now = time.time()
        for b in self.bases:
            cur, mid = self.current[b], self.ondo_mid(b)
            if cur is None or mid is None:
                continue
            for side, s in (("buy", cur.bid), ("sell", cur.ask)):
                if s is not None:
                    self.ledger.record_quote(now, b, side, s.price, s.size, mid, self.cfg.quoting.band_bps,
                                             st.quote_snapshot_sec)
        if now - last["basis"] >= st.basis_sample_sec:
            for s_ in self.board.states.values():
                self.ledger.record_sample(s_, now)
            last["basis"] = now
        self.ledger.commit()
        if self.live and now - last["equity"] >= 60:
            try:
                await self.update_equity()
                last["equity"] = now
            except Exception as e:
                log.warning("자산 조회 실패: %r", e)
        if now - last["status"] >= 60:
            last["status"] = now
            log.info(self.status_line(now))

    def status_line(self, now: float) -> str:
        mins = max((now - self.started) / 60, 1e-9)
        parts = [f"{'LIVE' if self.live else 'DRY'} req/min={self.requests_sent / mins:.1f}"]
        if self.killed:
            parts.append(f"KILLED({self.killed})")
        elif self.paused:
            parts.append(f"PAUSED({self.paused})")
        if self.equity_start is not None and self.equity_now is not None:
            parts.append(f"equity={self.equity_now:.2f} ({self.equity_now - self.equity_start:+.2f})")
        for b in self.bases:
            cur, mid = self.current[b], self.ondo_mid(b)
            bps = self.board.states[b].basis_bps()
            inv = self.inventory(b)
            q = "-" if cur is None else f"{cur.bid.price if cur.bid else '-'}/{cur.ask.price if cur.ask else '-'}"
            parts.append(f"{b}[mid={mid} q={q} inv={inv} basis={'-' if bps is None else f'{bps:.1f}'}]")
        return " | ".join(parts)

    # ---------- 실행 ----------

    async def run(self, duration: float | None) -> int:
        await self.setup()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except NotImplementedError:  # Windows: Ctrl+C는 KeyboardInterrupt로 온다
                pass
        ws_tasks = [asyncio.create_task(self.ondo_ws.run()), asyncio.create_task(self.lighter_ws.run())]
        loop_tasks: list[asyncio.Task] = []
        self.ledger.record_event("engine", "start", f"{'live' if self.live else 'dry_run'} {','.join(self.bases)}")
        try:
            await self._wait_for_quotes(timeout=20)
            if self.live:
                await self.live_startup()
                ws_tasks.append(asyncio.create_task(self.private_ws.run()))
                await self._wait_for_login(timeout=15)
                loop_tasks.append(asyncio.create_task(self.hedge_loop()))
            loop_tasks += [asyncio.create_task(self.quote_loop()), asyncio.create_task(self.risk_loop()),
                           asyncio.create_task(self.record_loop())]
            log.info("시작: %s", "실운영" if self.live else "dry_run (주문 없음)")
            if duration:
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=duration)
                except asyncio.TimeoutError:
                    pass
            else:
                await self.stop.wait()
            return 0
        finally:
            self.stop.set()
            # 호가 루프를 먼저 멈춰야 취소 직후 새 호가가 걸리지 않는다
            for t in loop_tasks:
                t.cancel()
            if loop_tasks:
                await asyncio.gather(*loop_tasks, return_exceptions=True)
            log.info("종료: Ondo 호가 전부 취소 (헷지된 포지션은 유지)")
            if not await self.cancel_all_quotes(attempts=3):
                log.error("종료 중 Ondo 호가 취소 3회 실패. 데드맨 스위치가 약 30초 뒤 취소하지만, Ondo 앱에서 직접 확인할 것")
            for ws in (self.ondo_ws, self.lighter_ws, self.private_ws):
                if ws is not None:
                    ws.stop()
            await asyncio.wait(ws_tasks, timeout=10)
            for t in ws_tasks:
                t.cancel()
            if self.trader is not None:
                await self.trader.close()
            self.ledger.record_event("engine", "stop", self.killed or "")
            self.ledger.commit()
            log.info(self.status_line(time.time()))

    async def _wait_for_quotes(self, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if all(s.ondo and s.lighter for s in self.board.states.values()):
                return
            await asyncio.sleep(0.5)
        missing = [b for b, s in self.board.states.items() if not (s.ondo and s.lighter)]
        raise RuntimeError(f"{timeout:.0f}초 안에 시세를 못 받음: {missing}")

    async def _wait_for_login(self, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.private_ws.ready:
                return
            await asyncio.sleep(0.5)
        if not self.private_ws.logged_in:
            raise RuntimeError("Ondo 비공개 WS 로그인 실패 (키와 IP 화이트리스트 확인)")
        raise RuntimeError("Ondo 데드맨 스위치 활성화 확인 실패 (로그에서 subscribed 응답 확인)")


def _log_task_error(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("백그라운드 작업 오류: %r", task.exception())
