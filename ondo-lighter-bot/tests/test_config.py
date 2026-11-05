from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from olbot.config import ConfigError, load_config, load_secrets, uses_mainnet_trading, validate
from olbot.main import main

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


def write_cfg(tmp_path, mutate=None):
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    if mutate:
        mutate(raw)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return p


def test_example_config_loads_and_validates():
    cfg = load_config(EXAMPLE)
    assert validate(cfg) == []
    assert cfg.mode == "dry_run"
    assert cfg.bases == ["LIT", "NEAR", "ENA", "SOL"]
    assert isinstance(cfg.markets[0].size_usd_per_side, Decimal)
    assert isinstance(cfg.risk.max_unhedged_usd, Decimal)
    assert not uses_mainnet_trading(cfg)  # 예시는 샌드박스/테스트넷이 기본


def test_unknown_key_is_rejected(tmp_path):
    p = write_cfg(tmp_path, lambda r: r["quoting"].update(ofset_bps=10))
    with pytest.raises(ConfigError, match="ofset_bps"):
        load_config(p)


def test_unknown_top_level_section_is_rejected(tmp_path):
    p = write_cfg(tmp_path, lambda r: r.update(qouting={}))
    with pytest.raises(ConfigError, match="qouting"):
        load_config(p)


def test_wrong_type_is_rejected(tmp_path):
    p = write_cfg(tmp_path, lambda r: r["ondo"].update(leverage="3"))
    with pytest.raises(ConfigError, match="ondo.leverage"):
        load_config(p)


def test_missing_file_explains_how_to_create(tmp_path):
    with pytest.raises(ConfigError, match="cp config.example.yaml"):
        load_config(tmp_path / "nope.yaml")


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda r: r["quoting"].update(offset_bps=30), "offset_bps"),
        (lambda r: r["quoting"].update(offset_bps=22, requote_threshold_bps=6), "band 밖"),
        (lambda r: r.update(mode="yolo"), "mode"),
        (lambda r: r["markets"].append(dict(r["markets"][0])), "중복"),
        (lambda r: r["markets"][0].update(max_inventory_usd=100), "max_inventory_usd"),
        (lambda r: r["ondo"].update(deadman_heartbeat_sec=30), "deadman"),
        (lambda r: r["ondo"].update(leverage=20), "ondo.leverage"),
        (lambda r: r["lighter"].update(ws_url="ws://insecure"), "wss://"),
        (lambda r: r["quoting"].update(basis_deviation_bps=200), "basis_deviation_bps"),
        (lambda r: r["markets"][0].update(basis_deviation_bps="wide"), "markets.LIT.basis_deviation_bps"),
    ],
)
def test_validation_catches_inconsistent_values(tmp_path, mutate, needle):
    errs = validate(load_config(write_cfg(tmp_path, mutate)))
    assert any(needle in e for e in errs), errs


def test_mainnet_detection(tmp_path):
    p = write_cfg(tmp_path, lambda r: r["ondo"].update(rest_url="https://api.ondoperps.xyz"))
    assert uses_mainnet_trading(load_config(p))


def test_secrets_never_leak_in_repr(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ONDO_API_KEY_ID=ondoKeyId_abc\nONDO_API_SECRET=ondoApiSecret_supersecret\n")
    s = load_secrets(env)
    assert s.ondo_api_secret == "ondoApiSecret_supersecret"
    assert "supersecret" not in repr(s)
    assert s.present()["ondo_api_key_id"] is True


def test_placeholder_secrets_count_as_empty(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ONDO_API_KEY_ID=ondoKeyId_\nONDO_API_SECRET=ondoApiSecret_\n")
    s = load_secrets(env)
    assert not s.present()["ondo_api_key_id"]
    assert not s.present()["ondo_api_secret"]


def test_check_config_cli_passes_on_example(capsys, tmp_path):
    rc = main(["--config", str(EXAMPLE), "--env", str(tmp_path / "missing.env"), "check-config"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "검증 통과" in out


def test_check_config_cli_fails_on_bad_config(capsys, tmp_path):
    p = write_cfg(tmp_path, lambda r: r["quoting"].update(offset_bps=40))
    rc = main(["--config", str(p), "check-config"])
    assert rc == 1
    assert "검증 실패" in capsys.readouterr().out
