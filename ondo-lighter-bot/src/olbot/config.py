"""설정(config.yaml)과 비밀값(.env) 로딩, 검증.

설정 키에 오타가 있으면 조용히 무시하지 않고 에러로 알린다.
금액(*_usd)은 Decimal, bps와 초 단위 값은 float/int로 읽는다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values


class ConfigError(Exception):
    pass


@dataclass
class OndoCfg:
    rest_url: str
    ws_url: str
    rest_rate_per_sec: float = 1.0
    rest_reserved_per_min: int = 10
    leverage: int = 3
    deadman_timeout_sec: int = 30
    deadman_heartbeat_sec: int = 10


@dataclass
class LighterCfg:
    base_url: str
    ws_url: str
    leverage: int = 3


@dataclass
class MarketDataCfg:
    """시세 수신용 공개 엔드포인트. 읽기 전용이라 메인넷을 쓴다."""
    ondo_rest_url: str = "https://api.ondoperps.xyz"
    ondo_ws_url: str = "wss://api.ondoperps.xyz/ws"
    lighter_rest_url: str = "https://mainnet.zklighter.elliot.ai"
    lighter_ws_url: str = "wss://mainnet.zklighter.elliot.ai/stream?readonly=true"
    ondo_ping_sec: float = 30.0
    lighter_ping_sec: float = 60.0


@dataclass
class QuotingCfg:
    band_bps: float = 25.0
    offset_bps: float = 18.0
    requote_threshold_bps: float = 6.0
    skew_bps_per_1k: float = 2.0
    # 두 거래소 사이에는 평소 괴리가 꾸준히 있다(2026-10-27 실측 6~31bps).
    # 헷지 손익을 흔드는 건 괴리의 크기가 아니라 변화라서, 최근 중앙값에서 벗어난 정도로 판단한다.
    basis_deviation_bps: float = 15.0
    basis_window_sec: float = 3600.0
    # 이걸 넘으면 같은 심볼이라도 다른 자산으로 보고 아예 제외한다 (예: OPENAI 약 477bps)
    basis_sanity_bps: float = 100.0
    quote_when_underlying_closed: bool = False
    inventory_mode: str = "flatten"


@dataclass
class HedgingCfg:
    max_slippage_bps: float = 15.0
    retries: int = 3
    reconcile_interval_sec: float = 10.0
    ondo_rest_reconcile_sec: float = 30.0


@dataclass
class RiskCfg:
    max_unhedged_usd: Decimal = Decimal("300")
    max_unhedged_sec: float = 20.0
    ws_stale_sec: float = 10.0
    max_margin_usage: float = 0.5
    daily_loss_limit_usd: Decimal = Decimal("100")
    max_total_inventory_usd: Decimal = Decimal("5000")


@dataclass
class MarketCfg:
    base: str
    size_usd_per_side: Decimal
    max_inventory_usd: Decimal


@dataclass
class StorageCfg:
    sqlite_path: str = "data/olbot.sqlite"
    log_path: str = "data/olbot.log"
    quote_snapshot_sec: float = 30.0
    band_depth_sec: float = 300.0
    basis_sample_sec: float = 10.0


@dataclass
class Config:
    mode: str
    ondo: OndoCfg
    lighter: LighterCfg
    quoting: QuotingCfg
    hedging: HedgingCfg
    risk: RiskCfg
    markets: list[MarketCfg]
    storage: StorageCfg
    market_data: MarketDataCfg = field(default_factory=MarketDataCfg)
    path: Path | None = None

    @property
    def bases(self) -> list[str]:
        return [m.base for m in self.markets]


@dataclass
class Secrets:
    ondo_api_key_id: str = ""
    ondo_api_secret: str = ""
    lighter_api_key_private_key: str = ""
    lighter_account_index: str = ""
    lighter_api_key_index: str = ""

    def present(self) -> dict[str, bool]:
        return {f.name: bool(getattr(self, f.name)) for f in fields(self)}

    def __repr__(self) -> str:  # 비밀값이 로그나 예외에 찍히지 않게 한다
        flags = ", ".join(f"{k}={'set' if v else 'empty'}" for k, v in self.present().items())
        return f"Secrets({flags})"


def _coerce(value: Any, typ: Any, where: str) -> Any:
    if typ is Decimal or typ == "Decimal":
        try:
            return Decimal(str(value))
        except InvalidOperation:
            raise ConfigError(f"{where}: 숫자가 아님 ({value!r})")
    if typ is bool or typ == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: true/false 여야 함 ({value!r})")
        return value
    if typ is int or typ == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where}: 정수여야 함 ({value!r})")
        return value
    if typ is float or typ == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: 숫자여야 함 ({value!r})")
        return float(value)
    if typ is str or typ == "str":
        if not isinstance(value, str):
            raise ConfigError(f"{where}: 문자열이어야 함 ({value!r})")
        return value
    return value


def _build(cls, data: Any, where: str):
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: 항목 묶음(mapping)이어야 함")
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ConfigError(f"{where}: 알 수 없는 키 {unknown}")
    kwargs = {}
    for name, f in known.items():
        if name in data:
            kwargs[name] = _coerce(data[name], f.type, f"{where}.{name}")
    try:
        return cls(**kwargs)
    except TypeError as e:
        raise ConfigError(f"{where}: 필수 키 누락 ({e})")


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"{path} 없음. config.example.yaml을 복사해서 만든다: cp config.example.yaml {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("설정 파일 최상위는 mapping이어야 함")
    sections = {"mode", "ondo", "lighter", "quoting", "hedging", "risk", "markets", "storage", "market_data"}
    unknown = sorted(set(raw) - sections)
    if unknown:
        raise ConfigError(f"알 수 없는 최상위 키 {unknown}")
    markets_raw = raw.get("markets")
    if not isinstance(markets_raw, list) or not markets_raw:
        raise ConfigError("markets: 한 개 이상의 마켓 목록이 필요함")
    cfg = Config(
        mode=_coerce(raw.get("mode", "dry_run"), str, "mode"),
        ondo=_build(OndoCfg, raw.get("ondo"), "ondo"),
        lighter=_build(LighterCfg, raw.get("lighter"), "lighter"),
        quoting=_build(QuotingCfg, raw.get("quoting"), "quoting"),
        hedging=_build(HedgingCfg, raw.get("hedging"), "hedging"),
        risk=_build(RiskCfg, raw.get("risk"), "risk"),
        markets=[_build(MarketCfg, m, f"markets[{i}]") for i, m in enumerate(markets_raw)],
        storage=_build(StorageCfg, raw.get("storage"), "storage"),
        market_data=_build(MarketDataCfg, raw.get("market_data"), "market_data"),
        path=path,
    )
    for m in cfg.markets:
        m.base = m.base.strip().upper()
    return cfg


def validate(cfg: Config) -> list[str]:
    """설정 값끼리의 모순을 찾아 사람이 읽을 메시지 목록으로 돌려준다. 빈 목록이면 통과."""
    errs: list[str] = []
    q = cfg.quoting
    if cfg.mode not in ("dry_run", "live"):
        errs.append(f"mode는 dry_run 또는 live (현재 {cfg.mode!r})")
    if not 0 < q.offset_bps < q.band_bps:
        errs.append(f"quoting.offset_bps({q.offset_bps})는 0보다 크고 band_bps({q.band_bps})보다 작아야 함")
    if q.requote_threshold_bps <= 0:
        errs.append("quoting.requote_threshold_bps는 0보다 커야 함")
    if q.offset_bps + q.requote_threshold_bps > q.band_bps:
        errs.append(
            f"offset_bps + requote_threshold_bps({q.offset_bps + q.requote_threshold_bps})가 "
            f"band_bps({q.band_bps})를 넘으면 재호가 전에 band 밖으로 나간다"
        )
    if not 0 < q.basis_deviation_bps < q.basis_sanity_bps:
        errs.append("quoting.basis_deviation_bps는 0보다 크고 basis_sanity_bps보다 작아야 함")
    if q.basis_window_sec < 60:
        errs.append("quoting.basis_window_sec는 60초 이상으로 둔다")
    if q.inventory_mode not in ("flatten", "hold_for_funding"):
        errs.append(f"quoting.inventory_mode는 flatten 또는 hold_for_funding (현재 {q.inventory_mode!r})")
    for name, lev in (("ondo.leverage", cfg.ondo.leverage), ("lighter.leverage", cfg.lighter.leverage)):
        if not 1 <= lev <= 5:
            errs.append(f"{name}({lev})는 1~5 범위로 둔다")
    if cfg.ondo.deadman_heartbeat_sec >= cfg.ondo.deadman_timeout_sec:
        errs.append("ondo.deadman_heartbeat_sec는 deadman_timeout_sec보다 짧아야 함")
    if cfg.ondo.rest_rate_per_sec <= 0:
        errs.append("ondo.rest_rate_per_sec는 0보다 커야 함")
    if cfg.ondo.rest_reserved_per_min >= cfg.ondo.rest_rate_per_sec * 60:
        errs.append("ondo.rest_reserved_per_min이 분당 전체 요청 수 이상이면 호가에 쓸 요청이 없다")
    seen: set[str] = set()
    for m in cfg.markets:
        if m.base in seen:
            errs.append(f"markets: {m.base} 중복")
        seen.add(m.base)
        if m.size_usd_per_side <= 0:
            errs.append(f"markets.{m.base}.size_usd_per_side는 0보다 커야 함")
        if m.max_inventory_usd < m.size_usd_per_side:
            errs.append(f"markets.{m.base}.max_inventory_usd가 size_usd_per_side보다 작음")
    if cfg.risk.max_total_inventory_usd <= 0:
        errs.append("risk.max_total_inventory_usd는 0보다 커야 함")
    for name, url in (
        ("ondo.rest_url", cfg.ondo.rest_url),
        ("lighter.base_url", cfg.lighter.base_url),
        ("market_data.ondo_rest_url", cfg.market_data.ondo_rest_url),
        ("market_data.lighter_rest_url", cfg.market_data.lighter_rest_url),
    ):
        if not url.startswith("https://"):
            errs.append(f"{name}는 https:// 로 시작해야 함")
    for name, url in (
        ("ondo.ws_url", cfg.ondo.ws_url),
        ("lighter.ws_url", cfg.lighter.ws_url),
        ("market_data.ondo_ws_url", cfg.market_data.ondo_ws_url),
        ("market_data.lighter_ws_url", cfg.market_data.lighter_ws_url),
    ):
        if not url.startswith("wss://"):
            errs.append(f"{name}는 wss:// 로 시작해야 함")
    return errs


def uses_mainnet_trading(cfg: Config) -> bool:
    return "sandbox" not in cfg.ondo.rest_url or "testnet" not in cfg.lighter.base_url


def load_secrets(env_path: str | Path | None = None) -> Secrets:
    """.env 파일 값을 먼저, 없으면 환경변수를 쓴다. os.environ은 건드리지 않는다."""
    file_vals = dotenv_values(env_path) if env_path and Path(env_path).exists() else {}

    def get(key: str) -> str:
        return (file_vals.get(key) or os.environ.get(key) or "").strip()

    s = Secrets(
        ondo_api_key_id=get("ONDO_API_KEY_ID"),
        ondo_api_secret=get("ONDO_API_SECRET"),
        lighter_api_key_private_key=get("LIGHTER_API_KEY_PRIVATE_KEY"),
        lighter_account_index=get("LIGHTER_ACCOUNT_INDEX"),
        lighter_api_key_index=get("LIGHTER_API_KEY_INDEX"),
    )
    # .env.example의 접두어만 남은 자리표시자는 비어 있는 것으로 본다
    if s.ondo_api_key_id == "ondoKeyId_":
        s.ondo_api_key_id = ""
    if s.ondo_api_secret == "ondoApiSecret_":
        s.ondo_api_secret = ""
    return s
