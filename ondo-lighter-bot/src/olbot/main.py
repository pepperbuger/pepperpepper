"""실행 진입점.

  python -m olbot check-config [--online]
  python -m olbot watch [--duration 3600] [--markets LIT,NEAR]
  python -m olbot run [--live] [--duration 3600]
  python -m olbot report [--hours 24] [--add-reward 시작일 종료일 USDC 포인트]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
from pathlib import Path

from olbot.config import ConfigError, load_config, load_secrets, uses_mainnet_trading, validate, validate_live


def setup_logging(log_path: str | None, verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_path, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def cmd_check_config(args) -> int:
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"설정 오류: {e}")
        return 2
    errs = validate(cfg)
    secrets = load_secrets(args.env)

    print(f"설정 파일: {cfg.path}")
    print(f"모드: {cfg.mode}")
    print(f"Ondo 거래 엔드포인트: {cfg.ondo.rest_url}")
    print(f"Lighter 거래 엔드포인트: {cfg.lighter.base_url}")
    if uses_mainnet_trading(cfg):
        print("주의: 거래 엔드포인트에 메인넷이 포함돼 있다. 실제 돈이 움직인다.")
    print(f"시세 엔드포인트(읽기 전용): {cfg.market_data.ondo_ws_url}, {cfg.market_data.lighter_ws_url}")
    print("마켓:")
    for m in cfg.markets:
        print(f"  {m.base:6} 한쪽 {m.size_usd_per_side} USD, 최대 재고 {m.max_inventory_usd} USD")
    print("비밀값(.env):")
    for k, v in secrets.present().items():
        print(f"  {k:30} {'있음' if v else '비어 있음'}")
    if not all(secrets.present().values()):
        print("  (1~2단계는 키 없이 동작한다. 3단계부터 필요)")

    if args.online:
        from olbot.markets import resolve

        md = cfg.market_data
        try:
            r = asyncio.run(resolve(cfg.bases, md.ondo_rest_url, md.lighter_rest_url))
        except Exception as e:
            print(f"마켓 확인 실패(네트워크): {e!r}")
            return 2
        print("마켓 확인(메인넷 공개 API):")
        for base in cfg.bases:
            print(f"  {base:6} Ondo {r.ondo_markets.get(base, '없음'):14} Lighter id {r.lighter_ids.get(base, '없음')}")
        errs.extend(r.problems)

    if errs:
        print("검증 실패:")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("검증 통과")
    return 0


def cmd_watch(args) -> int:
    from olbot.watch import run_watch

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"설정 오류: {e}")
        return 2
    errs = validate(cfg)
    if errs:
        for e in errs:
            print(f"설정 검증 실패: {e}")
        return 1
    setup_logging(cfg.storage.log_path, args.verbose)
    bases = [b.strip().upper() for b in args.markets.split(",")] if args.markets else cfg.bases
    return asyncio.run(run_watch(cfg, bases, args.duration, args.report_every))


def cmd_run(args) -> int:
    from olbot.engine import Engine
    from olbot.ledger import Ledger

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"설정 오류: {e}")
        return 2
    errs = validate(cfg)
    secrets = load_secrets(args.env)
    if args.live:
        errs += validate_live(cfg, secrets)
    if errs:
        for e in errs:
            print(f"실행 불가: {e}")
        return 1
    if cfg.mode == "live" and not args.live:
        print("config는 live지만 --live 플래그가 없어 dry_run으로 실행한다 (주문 없음)")
    setup_logging(cfg.storage.log_path, args.verbose)
    ledger = Ledger(cfg.storage.sqlite_path)
    try:
        return asyncio.run(Engine(cfg, secrets, args.live, ledger).run(args.duration))
    except KeyboardInterrupt:
        return 0
    finally:
        ledger.close()


def cmd_report(args) -> int:
    import time as _time

    from olbot.ledger import Ledger
    from olbot.report import build_report

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"설정 오류: {e}")
        return 2
    ledger = Ledger(cfg.storage.sqlite_path)
    try:
        if args.add_reward:
            week_start, week_end, usdc, points = args.add_reward
            ledger.add_reward(week_start, week_end, usdc, points, args.note or "")
            print("리워드 기록 완료")
        print(build_report(ledger, _time.time() - args.hours * 3600))
    finally:
        ledger.close()
    return 0


def cmd_pending(args) -> int:
    """미확정 헷지 기록 보기, 주문 상태 확인(조회만), 사람이 확인한 뒤 정리."""
    import datetime

    from olbot.ledger import Ledger, pending_scope

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"설정 오류: {e}")
        return 2
    secrets = load_secrets(args.env)
    if not secrets.lighter_account_index:
        print(".env에 LIGHTER_ACCOUNT_INDEX가 필요하다")
        return 2
    scope = pending_scope(cfg.lighter.base_url, int(secrets.lighter_account_index))
    ledger = Ledger(cfg.storage.sqlite_path)
    try:
        pending = ledger.load_pending_hedges(scope)
        if args.action == "list" or (args.action == "check" and not pending):
            if not pending:
                print("미확정 헷지 없음")
            for base, p in sorted(pending.items()):
                sent = datetime.datetime.fromtimestamp(p.sent_ts).strftime("%Y-%m-%d %H:%M:%S")
                print(f"{base:6} {'매도' if p.is_sell else '매수'} {p.size}  전송 {sent}  "
                      f"주문번호 {p.client_order_index if p.client_order_index is not None else '없음(예전 기록)'}  "
                      f"전송 전 Lighter 포지션 {p.lighter_pos_before}")
            return 0
        if args.action == "check":
            return asyncio.run(_check_pending(cfg, secrets, pending))
        if args.action == "clear":
            base = (args.base or "").upper()
            if base not in pending:
                print(f"{base} 미확정 기록 없음")
                return 1
            if not args.yes:
                print("Lighter 앱에서 이 주문의 실제 체결 여부와 현재 포지션을 확인한 뒤 --yes를 붙여 다시 실행한다.")
                print("확인 없이 지우면 봇이 같은 헷지를 다시 보낼 수 있다.")
                return 1
            ledger.save_pending_hedge(scope, base, None)
            ledger.record_event("operator", "pending_cleared", f"{base} {pending[base]}")
            print(f"{base} 미확정 기록을 지웠다. 다음 실행부터 봇이 실제 포지션 차이로 다시 헷지한다.")
            return 0
    finally:
        ledger.close()
    return 2


async def _check_pending(cfg, secrets, pending) -> int:
    from olbot.lighter.client import LighterTrader

    t = LighterTrader(cfg.lighter.base_url, int(secrets.lighter_account_index),
                      int(secrets.lighter_api_key_index), secrets.lighter_api_key_private_key)
    await t.connect()
    try:
        for base, p in sorted(pending.items()):
            if p.client_order_index is None:
                print(f"{base:6} 주문번호 없는 예전 기록: Lighter 앱에서 직접 확인 후 pending clear")
                continue
            os = await t.order_status(p.client_order_index)
            if os is None:
                print(f"{base:6} 주문 {p.client_order_index}: 거래소에서 찾지 못함 (아직 반영 전이거나 전송되지 않음)")
            else:
                print(f"{base:6} 주문 {p.client_order_index}: {os.status}, 체결 {os.filled}/{os.initial}"
                      + (" -> 최종 상태. 봇을 다시 켜면 자동으로 정리된다" if os.terminal else " -> 아직 진행 중"))
    finally:
        await t.close()
    return 0


def cmd_verify_keys(args) -> int:
    from olbot.verify import verify_keys

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"설정 오류: {e}")
        return 2
    secrets = load_secrets(args.env)
    missing = [k for k, v in secrets.present().items() if not v]
    if missing:
        print(f".env에 비어 있는 값: {missing}")
        return 2
    print(f"Ondo {cfg.ondo.rest_url}, Lighter {cfg.lighter.base_url} 에 조회만 한다 (주문 없음)")
    results = asyncio.run(verify_keys(cfg, secrets))
    for name, ok, detail in results:
        print(f"  [{'OK' if ok else '실패'}] {name}: {detail}")
    return 0 if all(ok for _, ok, _ in results) else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="olbot", description="Ondo + Lighter 유동성 리워드 봇")
    p.add_argument("--config", default="config.yaml", help="설정 파일 (기본 config.yaml)")
    p.add_argument("--env", default=".env", help="비밀값 파일 (기본 .env)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check-config", help="설정과 비밀값을 검증한다 (주문 없음)")
    c.add_argument("--online", action="store_true", help="설정한 마켓이 두 거래소에 실제로 있는지 공개 API로 확인")
    c.set_defaults(func=cmd_check_config)

    w = sub.add_parser("watch", help="2단계: 두 거래소 시세를 받아 괴리를 기록한다 (주문 없음)")
    w.add_argument("--duration", type=float, default=None, help="실행 시간(초). 없으면 Ctrl+C까지")
    w.add_argument("--markets", help="설정 대신 쓸 기초자산 목록 (예: LIT,NEAR,ENA,SOL,XRP)")
    w.add_argument("--report-every", type=float, default=60.0, help="현황 출력 간격(초)")
    w.set_defaults(func=cmd_watch)

    r = sub.add_parser("run", help="봇 실행. --live가 없으면 dry_run (주문 없음, 키 불필요)")
    r.add_argument("--live", action="store_true", help="실제 주문. config의 mode: live와 .env 키가 모두 필요")
    r.add_argument("--duration", type=float, default=None, help="실행 시간(초). 없으면 Ctrl+C까지")
    r.set_defaults(func=cmd_run)

    rp = sub.add_parser("report", help="운영 결과 리포트")
    rp.add_argument("--hours", type=float, default=24 * 7, help="최근 몇 시간 (기본 168 = 1주)")
    rp.add_argument("--add-reward", nargs=4, metavar=("WEEK_START", "WEEK_END", "USDC", "POINTS"),
                    help="Ondo 앱에서 확인한 주간 리워드 기록. 예: 2026-10-27 2026-11-02 3.21 1500")
    rp.add_argument("--note", help="리워드 메모")
    rp.set_defaults(func=cmd_report)

    vk = sub.add_parser("verify-keys", help="실제 키로 인증만 확인 (조회만, 주문 없음)")
    vk.set_defaults(func=cmd_verify_keys)

    pd = sub.add_parser("pending", help="미확정 헷지 주문 보기/상태 확인/정리 (주문 없음)")
    pd.add_argument("action", choices=["list", "check", "clear"],
                    help="list: 기록 보기, check: Lighter에서 주문 상태 조회(조회만), clear: 사람이 확인한 뒤 기록 삭제")
    pd.add_argument("base", nargs="?", help="clear할 마켓 (예: LIT)")
    pd.add_argument("--yes", action="store_true", help="Lighter 앱에서 확인했다는 표시. clear에 필요")
    pd.set_defaults(func=cmd_pending)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
