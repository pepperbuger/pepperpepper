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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
