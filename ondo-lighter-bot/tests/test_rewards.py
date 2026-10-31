import copy
import json
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from olbot.config import Secrets, load_config
from olbot.engine import Engine
from olbot.ledger import Ledger
from olbot.quoter import QuoteSide, TargetQuotes
from olbot.reward_probe import depth_summary, observe, share_scenario
from olbot.rewards import load_policy

ROOT = Path(__file__).parent.parent
POLICY = ROOT / 'docs/rewards-2026-10-27.json'


def test_week_boundary_and_ineligible_lit_ena():
    p = load_policy(POLICY)
    assert p.block_reason(['BTC', 'ETH'], p.starts) is None
    assert p.block_reason(['BTC'], p.ends - .001) is None
    assert p.block_reason(['BTC'], p.ends)
    assert p.block_reason(['BTC'], p.starts - .001)
    assert 'LIT' in p.block_reason(['LIT', 'ENA'], p.starts)


@pytest.mark.parametrize('field,value', [('starts_utc', '2026-10-27T00:00:00'),
                                         ('ends_utc', '2026-10-26T00:00:00+00:00'),
                                         ('per_market_usdc', 'NaN'), ('allowed_bases', ['btc'])])
def test_invalid_policy_rejected(tmp_path, field, value):
    raw = json.loads(POLICY.read_text(encoding='utf-8'))
    raw[field] = value
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    with pytest.raises(ValueError):
        load_policy(path)


def engine_with_policy():
    cfg = load_config(ROOT / 'config.live-500.example.yaml')
    cfg.rewards.policy_path = str(POLICY)
    # 기존 LIT 설정을 보호 장치로 걸러야 한다.
    return Engine(cfg, Secrets(), True, Ledger(':memory:'))


async def test_ineligible_setup_never_reaches_api(monkeypatch):
    eng = engine_with_policy()
    monkeypatch.setattr('olbot.engine.time.time', lambda: eng.reward_policy.starts + 10)
    resolve = AsyncMock(side_effect=AssertionError('network must not run'))
    monkeypatch.setattr('olbot.engine.resolve', resolve)
    try:
        with pytest.raises(RuntimeError, match='확인 대상 아님'):
            await eng.setup()
        resolve.assert_not_awaited()
    finally:
        eng.ledger.close()


async def test_expired_policy_kills_quotes_once_and_retains_hedger(monkeypatch):
    eng = engine_with_policy()
    eng.bases = ['BTC']
    eng.hedger = object()
    original = eng.hedger
    monkeypatch.setattr('olbot.engine.time.time', lambda: eng.reward_policy.ends)
    eng.cancel_all_quotes = AsyncMock(return_value=True)
    try:
        await eng._risk_tick()
        await eng._risk_tick()
        assert eng.killed and eng.hedger is original
        eng.cancel_all_quotes.assert_awaited_once()
    finally:
        eng.ledger.close()


async def test_expiry_during_cancel_budget_wait_does_not_create(monkeypatch):
    eng = engine_with_policy()
    eng.bases = ['BTC']
    eng.ondo_meta = {'BTC': {'market': 'BTC-USD.P'}}
    eng.current['BTC'] = None
    eng.order_ids['BTC'] = {}
    eng.ondo_rest = type('Fake', (), {'cancel_all': AsyncMock(), 'create_batch': AsyncMock()})()
    monkeypatch.setattr('olbot.engine.time.time', lambda: eng.reward_policy.ends)
    try:
        await eng.place('BTC', TargetQuotes(QuoteSide(D(100), D(1)), None))
        eng.ondo_rest.create_batch.assert_not_awaited()
    finally:
        eng.ledger.close()


@pytest.mark.parametrize('venue', ['ondo', 'lighter'])
async def test_existing_unmanaged_positions_block_before_cancel_or_leverage(monkeypatch, venue):
    eng = engine_with_policy()
    eng.bases = ['BTC']
    eng.ondo_meta = {'BTC': {'market': 'BTC-USD.P'}}
    eng.lighter_markets = {'BTC': type('Meta', (), {'market_id': 1})()}
    eng.ondo_rest = type('Rest', (), {'positions': AsyncMock(return_value={'LIT-USD.P': D(40)} if venue == 'ondo' else {}),
                                     'cancel_all': AsyncMock(), 'set_leverage': AsyncMock()})()
    eng.trader = type('Trader', (), {'connect': AsyncMock(),
                                     'positions': AsyncMock(return_value={120: D(-40)} if venue == 'lighter' else {})})()
    monkeypatch.setattr('olbot.engine.time.time', lambda: eng.reward_policy.starts + 10)
    try:
        with pytest.raises(RuntimeError, match='기존 포지션'):
            await eng.live_startup()
        eng.ondo_rest.cancel_all.assert_not_awaited()
        eng.ondo_rest.set_leverage.assert_not_awaited()
    finally:
        eng.ledger.close()


def test_decimal_real_captured_book_and_share_scenario():
    captured = json.loads((ROOT / 'tests/fixtures/reward-depth-eth.json').read_text())
    result = depth_summary(captured)
    assert result['mid'] == D('2261.45')
    assert result['outer_11_25_usd'] == D('52202.3519')
    assert result['depth_complete'] is True
    assert D('.57') < share_scenario(result['outer_11_25_usd'], D(120), D(250)) < D('.58')


def test_truncated_depth_is_not_a_complete_competitor_denominator():
    book = {'success': True, 'result': {'bids': [['99.99', '1']] * 100,
                                       'asks': [['100.01', '1']] * 100}}
    assert depth_summary(book)['depth_complete'] is False
    for p in ('NaN', 'Infinity', '-1'):
        bad = copy.deepcopy(book)
        bad['result']['bids'][0][0] = p
        with pytest.raises(ValueError):
            depth_summary(bad)


def test_expired_observation_has_no_reward_scenario(monkeypatch):
    policy = load_policy(POLICY)
    book = {'success': True, 'result': {'bids': [['99.99', '1']], 'asks': [['100.01', '1']]}}
    def get(url):
        assert '/api/v1/orderBooks' in url or '/v1/markets' in url or '/v1/perps/depth?' in url
        if '/api/v1/orderBooks' in url:
            return {'order_books': [{'symbol': 'BTC', 'status': 'active', 'market_type': 'perp'}]}
        if '/v1/markets' in url:
            return {'result': {'perps': {'tradingPairs': [{'pair': {'base': 'BTC'}, 'market': 'BTC-USD.P'}]}}}
        return book
    monkeypatch.setattr('olbot.reward_probe._get_json', get)
    monkeypatch.setattr('olbot.reward_probe.time.time', lambda: policy.ends)
    monkeypatch.setattr('olbot.reward_probe.time.sleep', lambda _: None)
    result = observe(policy, ['BTC'])
    assert result['policy_valid'] is False and result['orders_sent'] == 0
    assert 'both_60_share_scenario_week_usdc' not in result['rows'][0]
