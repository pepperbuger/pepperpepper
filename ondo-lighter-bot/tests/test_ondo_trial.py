from decimal import Decimal
import asyncio
import json
from types import SimpleNamespace

import pytest

from olbot import ondo_trial as trial


META = {'baseIncrement':'1','quoteIncrement':'.0001'}


def raw_order(**changes):
    return {'clientOrderId':'probe_a','market':'LIT-USD.P','side':'buy','type':'limit',
            'timeInForce':'GTC','reduceOnly':False,'size':'1','filledSize':'0','status':'canceled',
            'cancelReason':'cancelAfterTimeout', **changes}


def test_small_plan_post_only_and_cap():
    plan = trial.calculate_plan(META, '2.9', '2.91')
    payload = trial.entry_payload(plan, 'probe_a')
    assert payload['size'] == '1' and payload['postOnly'] is True
    assert payload['timeInForce'] == 'GTC' and Decimal(payload['price']) == Decimal('2.61')
    with pytest.raises(ValueError):
        trial.calculate_plan(META, '5.1', '5.2')
    with pytest.raises(ValueError):
        trial.entry_payload({**plan,'price':'6'}, 'probe_a')


@pytest.mark.parametrize('change', [{'clientOrderId':'other'}, {'market':'BTC-USD.P'},
    {'side':'sell'}, {'filledSize':None}, {'filledSize':'NaN'}, {'filledSize':'2'},
    {'status':'fullyfilled','filledSize':'0'}, {'status':'other'}, {'reduceOnly':0}, {'size':'2'}])
def test_order_schema_errors_never_count_as_zero_fill(change):
    with pytest.raises((ValueError, ArithmeticError, KeyError)):
        trial.parse_order(raw_order(**change), 'probe_a', '1')


def test_ws_parser_only_accepts_expected_acknowledgment():
    assert trial.parse_ws_event({'type':'loggedIn'}) == {'type':'loggedIn'}
    assert trial.parse_ws_event({'type':'subscribed','channel':'cancelAllOrdersAfterPerps'})
    assert trial.parse_ws_event({'type':'subscribed','channel':'positionsPerps'}) is None
    assert trial.parse_ws_event({'type':'update','channel':'cancelAllOrdersAfterPerps','data':{}}) is None


def initial(path):
    state = {'phase':'resting','entry_id':'probe_a','resting_confirmed':True,'worker_terminated':True,
             'plan':{'quote_increment':'.0001','base_increment':'1'}}
    trial.save(path, state, first=True)
    return state


def clock():
    value = [0.0]
    async def sleep(seconds):
        value[0] += seconds
    return lambda:value[0], sleep


class Rest:
    def __init__(self, path, orders):
        self.path, self.orders, self.calls = path, list(orders), []

    async def request(self, method, route, params=None, payload=None):
        self.calls.append((method, route, payload))
        if method == 'POST':
            saved = json.loads(self.path.read_text())
            assert saved['phase'] == 'close_pending'
            assert payload['clientOrderId'] == saved['close_id']
            return raw_order(**payload)
        if method == 'DELETE':
            assert route == trial.ORDER_PATH + 'probe_a'
            return raw_order(cancelReason='')
        if route == '/v1/perps/positions':
            return []
        if route == '/v1/perps/orders':
            return []
        if not self.orders:
            raise OSError('unknown')
        item = self.orders.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def test_deadman_pass_needs_positive_cancellation_evidence(tmp_path):
    path = tmp_path/'probe.json'
    state = initial(path)
    rest = Rest(path, [raw_order()])
    now, sleep = clock()
    result = await trial.observe(rest, state, path, clock=now, sleep=sleep)
    assert result['resolved'] and result['deadman_verified']
    assert not any(method != 'GET' for method,_,_ in rest.calls)


async def test_timeout_cancellation_fallback_is_not_a_deadman_pass(monkeypatch, tmp_path):
    path = tmp_path/'probe.json'
    state = initial(path)
    rest = Rest(path, [])
    reads = [0]
    async def query(*args):
        reads[0] += 1
        if reads[0] <= 21:
            return {'status':'open','filled':'0','deadman_reason':False}
        return {'status':'canceled','filled':'0','deadman_reason':False}
    monkeypatch.setattr(trial, 'query_entry', query)
    now, sleep = clock()
    result = await trial.observe(rest, state, path, clock=now, sleep=sleep)
    assert result['resolved'] and not result['deadman_verified']
    deletes = [route for method,route,_ in rest.calls if method == 'DELETE']
    assert deletes == [trial.ORDER_PATH + 'probe_a']


async def test_unknown_order_is_not_recreated(tmp_path):
    path = tmp_path/'probe.json'
    state = initial(path)
    rest = Rest(path, [])
    now, sleep = clock()
    result = await trial.observe(rest, state, path, clock=now, sleep=sleep)
    assert not result['resolved']
    assert sum(method == 'POST' for method,_,_ in rest.calls) == 0
    assert sum(method == 'DELETE' for method,_,_ in rest.calls) == 3


async def test_fill_cleanup_is_exact_reduce_only_once(monkeypatch, tmp_path):
    path = tmp_path/'probe.json'
    state = initial(path)
    rest = Rest(path, [raw_order(status='fullyfilled',filledSize='1'),
        raw_order(clientOrderId='probe_a_close',side='sell',timeInForce='IOC',reduceOnly=True,
                  status='fullyfilled',filledSize='1')])
    counts = [0]
    async def positions(_):
        counts[0] += 1
        return {'LIT-USD.P':Decimal(1)} if counts[0] == 1 else {}
    async def quote():
        return Decimal('2.9'), Decimal('2.91')
    monkeypatch.setattr(trial, 'positions', positions)
    monkeypatch.setattr(trial, 'quote', quote)
    now, sleep = clock()
    result = await trial.observe(rest, state, path, clock=now, sleep=sleep)
    assert result['resolved'] and not result['deadman_verified']
    posts = [payload for method,_,payload in rest.calls if method == 'POST']
    assert len(posts) == 1 and posts[0]['reduceOnly'] is True and posts[0]['size'] == '1'


def test_live_guard_rejects_resolved_but_failed_deadman_test(tmp_path):
    cfg = SimpleNamespace(storage=SimpleNamespace(sqlite_path=tmp_path/'olbot.sqlite'))
    trial.save(trial.path_for(cfg), {'phase':'resolved','deadman_verified':False}, first=True)
    with pytest.raises(RuntimeError):
        trial.require_resolved_trial(cfg)


@pytest.mark.parametrize('cap,deadman,cleanup', [('5',False,True),('5',True,False),('6',True,True)])
async def test_execution_approval_flags_are_required(monkeypatch, tmp_path, cap, deadman, cleanup):
    cfg = SimpleNamespace(ondo=SimpleNamespace(rest_url=trial.BASE,ws_url=trial.WS),
                          storage=SimpleNamespace(sqlite_path=tmp_path/'olbot.sqlite'))
    monkeypatch.setattr(trial,'load_config',lambda path:cfg)
    monkeypatch.setattr(trial,'load_secrets',lambda path:None)
    args = SimpleNamespace(config='unused',env='unused',execute=True,approve_max_entry_usd=cap,
                           approve_deadman=deadman,approve_cleanup=cleanup)
    with pytest.raises(ValueError,match='승인 플래그'):
        await trial.run(args)


@pytest.mark.parametrize('acknowledged', [False, True])
async def test_worker_waits_for_deadman_ack_and_persists_before_post(monkeypatch, tmp_path, acknowledged):
    path = tmp_path/'trial.json'
    secrets = SimpleNamespace(ondo_api_key_id='test-key')
    state = {'phase':'prepared','entry_id':'probe_a','scope_hash':trial.scope(secrets),
             'plan':{'size':'1','price':'2.6'},'order_attempted':False}
    trial.save(path,state,first=True)
    calls=[]
    class FakeWS:
        def __init__(self, *args):
            self.ready=acknowledged
        async def run(self):
            await asyncio.Event().wait()
        def stop(self):
            pass
    class FakeRest:
        async def request(self, method, route, **kwargs):
            if method=='POST':
                saved=json.loads(path.read_text())
                assert saved['order_attempted'] is True and saved['phase']=='entry_pending'
                calls.append(kwargs['payload'])
            return raw_order(status='open',cancelReason='')
    monkeypatch.setattr(trial,'load_secrets',lambda _:secrets)
    monkeypatch.setattr(trial,'make_rest',lambda _:FakeRest())
    monkeypatch.setattr(trial,'ProbeWS',FakeWS)
    task=asyncio.create_task(trial.worker_main('unused',path))
    await asyncio.sleep(.03)
    task.cancel()
    await asyncio.gather(task,return_exceptions=True)
    assert len(calls)==int(acknowledged)


async def test_controller_terminates_worker_before_observation(monkeypatch,tmp_path):
    cfg=SimpleNamespace(ondo=SimpleNamespace(rest_url=trial.BASE,ws_url=trial.WS),
                        storage=SimpleNamespace(sqlite_path=tmp_path/'olbot.sqlite'))
    secrets=SimpleNamespace(ondo_api_key_id='test-key')
    class Process:
        def __init__(self, **kwargs):
            self.path=kwargs['args'][1]
            self.alive=False
            self.terminated=False
        def start(self):
            self.alive=True
            state=json.loads(open(self.path,encoding='utf-8').read())
            assert state['phase']=='prepared' and not state['order_attempted']
            state.update(phase='resting',order_attempted=True,resting_confirmed=True)
            trial.save(trial.Path(self.path),state)
        def is_alive(self):
            return self.alive
        def terminate(self):
            self.terminated=True
            self.alive=False
        def join(self,timeout):
            assert self.terminated and not self.alive
    async def preflight(_):
        return {'size':'1','price':'2.6','sufficient_margin':True,'no_positions':True,'no_active_orders':True}
    async def observe(rest,state,path):
        assert state['worker_terminated'] is True
        assert state['killed_at_monotonic']>0
        return {'resolved':False}
    monkeypatch.setattr(trial,'load_config',lambda _:cfg)
    monkeypatch.setattr(trial,'load_secrets',lambda _:secrets)
    monkeypatch.setattr(trial,'make_rest',lambda _:object())
    monkeypatch.setattr(trial,'preflight',preflight)
    monkeypatch.setattr(trial,'observe',observe)
    monkeypatch.setattr(trial,'multiprocessing',SimpleNamespace(get_context=lambda _:SimpleNamespace(Process=Process)))
    args=SimpleNamespace(config='unused',env='unused',execute=True,approve_max_entry_usd='5',
                          approve_deadman=True,approve_cleanup=True)
    assert not (await trial.run(args))['resolved']


async def test_late_cancel_is_not_reported_as_within_30_seconds(tmp_path):
    path=tmp_path/'probe.json'
    state=initial(path)
    state['killed_at_monotonic']=-31
    rest=Rest(path,[raw_order()])
    now,sleep=clock()
    result=await trial.observe(rest,state,path,clock=now,sleep=sleep)
    assert result['resolved'] and not result['deadman_verified']
    assert result['observed_cancel_after_sec']==31


def test_failed_deadman_has_failure_exit_status_even_if_order_is_resolved(monkeypatch):
    import sys
    async def run(args):
        return {'resolved':True,'deadman_verified':False}
    monkeypatch.setattr(trial,'run',run)
    monkeypatch.setattr(trial.logging,'disable',lambda level:None)
    monkeypatch.setattr(sys,'argv',['test','--execute','--approve-max-entry-usd','5',
                                  '--approve-deadman','--approve-cleanup'])
    assert trial.main()==1
