from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from olbot import trial
from olbot.lighter.client import OrderStatus


META = {'market_id': 17, 'supported_price_decimals': 4, 'supported_size_decimals': 2,
        'min_quote_amount': '10', 'min_base_amount': '.01'}


def plan():
    return {**trial.calculate_plan(META, '2.95', '2.96'),
            'no_existing_positions': True, 'sufficient_margin': True}


def test_minimum_is_rounded_up_but_entry_stays_below_approval_cap():
    p = plan()
    size, price = Decimal(p['size']), Decimal(p['price'])
    assert size * price >= Decimal('10.10')
    assert Decimal(p['reference_notional_usd']) <= 12
    assert price <= Decimal('2.95') * Decimal('.99')
    assert p['base_amount'] == size * 100
    with pytest.raises(ValueError, match='12 USD'):
        trial.calculate_plan({**META, 'min_quote_amount': '20'}, '2.95', '2.96')


@pytest.mark.parametrize('bid,ask', [('NaN', '3'), ('3', '3'), ('4', '3'), ('0', '3')])
def test_bad_book_prevents_a_plan(bid, ask):
    with pytest.raises(ValueError):
        trial.calculate_plan(META, bid, ask)


def fake_trader(path, lose_response=False):
    class Trader:
        def __init__(self):
            self.client = self
            self.calls = []
            self.last = 40
            self.account_index = 7

        def new_client_order_index(self):
            self.last += 1
            return self.last

        def _auth(self):
            return 'test-token'

        async def create_market_order(self, **kwargs):
            saved = json.loads(path.read_text(encoding='utf-8'))
            assert kwargs['client_order_index'] in (saved.get('entry_id'), saved.get('close_id'))
            self.calls.append(kwargs)
            if lose_response:
                raise OSError('response lost')
            return None, SimpleNamespace(code=200), None
    return Trader()


def wire(monkeypatch, final_results):
    results = iter(final_results)

    async def final(*args, **kwargs):
        return next(results)

    async def get(path, token=None):
        if 'accountActiveOrders' in path:
            return {'orders': []}
        return {'bids': [{'price': '2.94', 'remaining_base_amount': '10'}]}

    monkeypatch.setattr(trial, 'final_order', final)
    monkeypatch.setattr(trial, 'get', get)


async def test_unknown_response_sends_once_and_blocks_live(monkeypatch, tmp_path):
    path = tmp_path / 'lighter-order-trial.json'
    trader = fake_trader(path, lose_response=True)
    wire(monkeypatch, [None])
    result = await trial.execute(trader, plan(), path)
    assert not result['resolved'] and len(trader.calls) == 1
    cfg = SimpleNamespace(storage=SimpleNamespace(sqlite_path=tmp_path / 'olbot.sqlite'))
    with pytest.raises(RuntimeError, match='미확정'):
        trial.require_resolved_trial(cfg)
    with pytest.raises(FileExistsError):
        await trial.execute(trader, plan(), path)
    assert len(trader.calls) == 1


async def test_canceled_without_fill_finishes_without_cleanup(monkeypatch, tmp_path):
    path = tmp_path / 'lighter-order-trial.json'
    p = plan()
    wire(monkeypatch, [OrderStatus(41, 'canceled-too-much-slippage', Decimal(0), Decimal(p['size']))])
    trader = fake_trader(path)
    assert (await trial.execute(trader, p, path))['resolved']
    assert len(trader.calls) == 1
    cfg = SimpleNamespace(storage=SimpleNamespace(sqlite_path=tmp_path / 'olbot.sqlite'))
    trial.require_resolved_trial(cfg)


async def test_fill_is_closed_once_with_reduce_only_and_exact_quantity(monkeypatch, tmp_path):
    path = tmp_path / 'lighter-order-trial.json'
    p = plan()
    amount = Decimal(p['size'])
    wire(monkeypatch, [OrderStatus(41, 'filled', amount, amount), OrderStatus(42, 'filled', amount, amount)])
    trader = fake_trader(path)
    assert (await trial.execute(trader, p, path))['resolved']
    assert len(trader.calls) == 2
    close = trader.calls[1]
    assert close['reduce_only'] is True and close['is_ask'] is True
    assert close['base_amount'] == p['base_amount']


async def test_partial_cleanup_remains_unresolved_without_third_order(monkeypatch, tmp_path):
    path = tmp_path / 'lighter-order-trial.json'
    p = plan()
    amount = Decimal(p['size'])
    wire(monkeypatch, [OrderStatus(41, 'filled', amount, amount),
                       OrderStatus(42, 'canceled', Decimal('.01'), amount)])
    trader = fake_trader(path)
    assert not (await trial.execute(trader, p, path))['resolved']
    assert len(trader.calls) == 2
    assert json.loads(path.read_text())['phase'] == 'close_pending'


async def test_storage_failure_prevents_any_order(monkeypatch, tmp_path):
    path = tmp_path / 'lighter-order-trial.json'
    wire(monkeypatch, [])
    trader = fake_trader(path)
    def fail(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(trial, 'save', fail)
    with pytest.raises(OSError):
        await trial.execute(trader, plan(), path)
    assert not trader.calls


@pytest.mark.parametrize('field', ['sufficient_margin', 'no_existing_positions'])
async def test_preflight_blocks_transmission(field, tmp_path):
    path = tmp_path / 'lighter-order-trial.json'
    trader = fake_trader(path)
    with pytest.raises(ValueError):
        await trial.execute(trader, {**plan(), field: False}, path)
    assert not trader.calls and not path.exists()


@pytest.mark.parametrize('change', [None, {'owner_account_index': 9}, {'market_index': 99},
    {'is_ask': 0}, {'initial_base_amount': '20'}, {'status': 'canceled-unknown'},
    {'filled_base_amount': None}])
async def test_final_confirmation_requires_identity_and_strict_quantity(monkeypatch, change):
    p = plan()
    raw = {'client_order_index': 41, 'owner_account_index': 7, 'market_index': 17,
           'is_ask': False, 'status': 'canceled-too-much-slippage',
           'initial_base_amount': p['size'], 'filled_base_amount': '0'}
    if change:
        raw.update(change)
    clock = [0.0]
    async def sleep(seconds):
        clock[0] += seconds
    async def get(path, token=None):
        return {'orders': [raw]}
    async def account():
        return {'positions': []}
    trader = SimpleNamespace(account_index=7, _auth=lambda: 'test-token', account=account)
    monkeypatch.setattr(trial, 'get', get)
    monkeypatch.setattr(trial, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(trial, 'asyncio', SimpleNamespace(sleep=sleep))
    result = await trial.final_order(trader, p, 41, False, timeout=1)
    assert (result is not None) == (change is None)


@pytest.mark.parametrize('limit,cleanup', [(None, False), ('12', False), ('13', True)])
async def test_execute_flags_are_required_before_connect(monkeypatch, tmp_path, limit, cleanup):
    cfg = SimpleNamespace(lighter=SimpleNamespace(base_url=trial.BASE),
                          storage=SimpleNamespace(sqlite_path=tmp_path / 'olbot.sqlite'))
    monkeypatch.setattr(trial, 'load_config', lambda path: cfg)
    monkeypatch.setattr(trial, 'load_secrets', lambda path: None)
    args = SimpleNamespace(config='unused', env='unused', execute=True,
                           approve_max_entry_usd=limit, approve_cleanup=cleanup)
    with pytest.raises(ValueError, match='승인 플래그'):
        await trial.run(args)


async def test_engine_refuses_pending_trial_before_market_requests(tmp_path):
    from pathlib import Path
    from olbot.config import load_config, Secrets
    from olbot.engine import Engine
    from olbot.ledger import Ledger

    cfg = load_config(Path(__file__).parent.parent / 'config.live-500.example.yaml')
    cfg.storage.sqlite_path = str(tmp_path / 'olbot.sqlite')
    trial.save(trial.journal_path(cfg), {'phase': 'entry_pending'}, first=True)
    ledger = Ledger(':memory:')
    try:
        with pytest.raises(RuntimeError, match='미확정'):
            await Engine(cfg, Secrets(), True, ledger).setup()
    finally:
        ledger.close()


async def test_actual_canceled_ioc_response_confirms_zero_fill(monkeypatch):
    from pathlib import Path
    raw = json.loads((Path(__file__).parent / 'fixtures/lighter-canceled-ioc.json').read_text())
    order = trial.parse_order_status(raw)
    assert order.terminal and order.status == 'canceled-too-much-slippage'
    assert order.initial == Decimal('3.54') and order.filled == Decimal('0.00')
    async def get(path, token=None):
        assert 'client_order_indexes=41' in path
        return {'orders': [raw]}
    async def account():
        return {'positions': []}
    trader = SimpleNamespace(account_index=7, _auth=lambda: 'test-token', account=account)
    monkeypatch.setattr(trial, 'get', get)
    result = await trial.final_order(trader, {'market_id':120, 'size':'3.54'}, 41, False)
    assert result == order
