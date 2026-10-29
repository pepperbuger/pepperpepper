"""Ondo LIT 1개 호가의 데드맨 강제 종료 시험. 기본은 조회만 한다.

실행에는 최대 5 USD 진입, 계정 전체 데드맨, 비상 취소/정리 승인이 모두 필요하다.
실제 주문은 1건, 체결 시 reduce-only IOC 정리는 1건만 허용한다.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from decimal import Decimal, ROUND_DOWN
import hashlib
import io
import json
import logging
import multiprocessing
from pathlib import Path
import time
import uuid

from olbot.config import load_config, load_secrets
from olbot.market import MarketBoard
from olbot.markets import fetch_ondo_perps
from olbot.ondo.private_ws import OndoPrivateWS
from olbot.ondo.rest import OndoRest
from olbot.ondo.ws import OndoPublicWS
from olbot.ratelimit import TokenBucket
from olbot.trial import save

BASE = 'https://api.ondoperps.xyz'
WS = 'wss://api.ondoperps.xyz/ws'
MARKET = 'LIT-USD.P'
ORDER_PATH = '/v1/perps/orders/client:'
TERMINAL = {'canceled', 'fullyfilled'}


def path_for(cfg):
    return Path(cfg.storage.sqlite_path).parent / 'ondo-deadman-trial.json'


def require_resolved_trial(cfg):
    path = path_for(cfg)
    if path.exists():
        state = json.loads(path.read_text(encoding='utf-8'))
        if state.get('phase') != 'resolved' or state.get('deadman_verified') is not True:
            raise RuntimeError('Ondo 시험이 미확정이거나 데드맨 검증에 실패했다. ondo-deadman-trial.json을 보존하고 확인해야 한다')


def make_rest(secrets):
    return OndoRest(BASE, secrets.ondo_api_key_id, secrets.ondo_api_secret, TokenBucket(1, 1), timeout=10)


def scope(secrets):
    return hashlib.sha256((BASE + ':' + secrets.ondo_api_key_id).encode()).hexdigest()


def positive_decimal(value):
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError('양수 응답이 필요하다')
    return number


def parse_order(raw, client_id, size, side='buy', *, cleanup=False):
    if not isinstance(raw, dict):
        raise ValueError('주문 응답 형식 오류')
    if (raw['clientOrderId'] != client_id or raw['market'] != MARKET or raw['side'] != side or
            raw['type'] != 'limit' or raw['timeInForce'] != ('IOC' if cleanup else 'GTC') or
            type(raw['reduceOnly']) is not bool or raw['reduceOnly'] != cleanup):
        raise ValueError('주문 식별/속성 불일치')
    original = positive_decimal(raw['size'])
    filled = Decimal(str(raw['filledSize']))
    if original != Decimal(size) or not filled.is_finite() or not 0 <= filled <= original:
        raise ValueError('주문 수량 불일치')
    status = raw['status']
    if status not in {'open', 'pending', 'untriggered', 'canceled', 'fullyfilled'}:
        raise ValueError('알 수 없는 주문 상태')
    if status == 'fullyfilled' and filled != original:
        raise ValueError('전량 체결 상태와 수량 불일치')
    reason = raw.get('cancelReason')
    return {'status': status, 'filled': str(filled),
            'deadman_reason': reason == 'cancelAfterTimeout'}


def parse_ws_event(message):
    """키/계정 데이터 없이 필요한 ACK의 타입과 채널만 분리한다."""
    if message.get('type') == 'loggedIn':
        return {'type': 'loggedIn'}
    if message.get('type') == 'subscribed' and message.get('channel') == 'cancelAllOrdersAfterPerps':
        return {'type': 'subscribed', 'channel': 'cancelAllOrdersAfterPerps'}
    return None


class ProbeWS(OndoPrivateWS):
    def __init__(self, secrets, state, path):
        super().__init__(WS, secrets.ondo_api_key_id, secrets.ondo_api_secret, [MARKET], 30, 10, lambda fill: None)
        self.state, self.path = state, path

    def private_subscriptions(self):
        return [{'op': 'subscribe', 'channel': 'cancelAllOrdersAfterPerps', 'timeout_seconds': 30}]

    def on_message(self, msg, recv_ts):
        event = parse_ws_event(msg)
        if event is not None:
            self.state.setdefault('ws_ack_messages', []).append(event)
            save(self.path, self.state)
        super().on_message(msg, recv_ts)


async def quote():
    board = MarketBoard(['LIT'])
    client = OndoPublicWS(WS, board, 10)
    task = asyncio.create_task(client.run())
    try:
        async with asyncio.timeout(15):
            while board.states['LIT'].ondo is None:
                await asyncio.sleep(.1)
        q = board.states['LIT'].ondo
        if not 0 < q.bid < q.ask:
            raise ValueError('호가 오류')
        return q.bid, q.ask
    finally:
        client.stop()
        await asyncio.wait({task}, timeout=4)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def positions(rest):
    rows = await rest.request('GET', '/v1/perps/positions')
    if not isinstance(rows, list):
        raise ValueError('포지션 응답 형식 오류')
    result = {}
    for row in rows:
        amount = Decimal(str(row['netQuantity']))
        if not amount.is_finite() or row['direction'] not in {'long', 'short'} or row['market'] in result:
            raise ValueError('포지션 응답 오류')
        result[row['market']] = abs(amount) * (1 if row['direction'] == 'long' else -1)
    return result


def calculate_plan(meta, bid, ask):
    step, tick = positive_decimal(meta['baseIncrement']), positive_decimal(meta['quoteIncrement'])
    bid, ask = positive_decimal(bid), positive_decimal(ask)
    if not bid < ask or Decimal(1) % step or ask > 5:
        raise ValueError('LIT 1개가 단위 또는 5 USD 범위를 벗어남')
    price = (bid * Decimal('.90') / tick).to_integral_value(rounding=ROUND_DOWN) * tick
    if price <= 0:
        raise ValueError('호가가 0으로 내려감')
    return {'market': MARKET, 'size': '1', 'price': str(price), 'quote_increment': str(tick),
            'base_increment': str(step), 'reference_notional_usd': str(ask)}


async def preflight(rest):
    balance = await rest.balance()
    current = await positions(rest)
    orders = await rest.request('GET', '/v1/perps/orders', params={'limit': 1000})
    if not isinstance(orders, list) or len(orders) >= 1000:
        raise ValueError('주문 목록을 완전히 확인하지 못함')
    no_orders = all(row['status'] in TERMINAL for row in orders)
    await rest.bucket.acquire()
    meta = (await fetch_ondo_perps(BASE))['LIT']
    bid, ask = await quote()
    plan = calculate_plan(meta, bid, ask)
    plan.update(sufficient_margin=Decimal(str(balance['availableMargin'])) >= 15,
                no_positions=not any(current.values()), no_active_orders=no_orders)
    return plan


def entry_payload(plan, client_id):
    if Decimal(plan['size']) != 1 or positive_decimal(plan['price']) * Decimal(plan['size']) > 5:
        raise ValueError('승인 범위 초과')
    return {'market': MARKET, 'side': 'buy', 'type': 'limit', 'size': '1', 'price': plan['price'],
            'timeInForce': 'GTC', 'postOnly': True, 'reduceOnly': False, 'clientOrderId': client_id}


async def worker_main(env_path, path):
    state = json.loads(path.read_text(encoding='utf-8'))
    secrets = load_secrets(env_path)
    if state['scope_hash'] != scope(secrets):
        return
    rest = make_rest(secrets)
    client = ProbeWS(secrets, state, path)
    task = asyncio.create_task(client.run())
    try:
        async with asyncio.timeout(15):
            while not client.ready:
                await asyncio.sleep(.1)
        payload = entry_payload(state['plan'], state['entry_id'])
        state.update(deadman_armed=True, phase='entry_pending', order_attempted=True)
        save(path, state)
        try:
            raw = await rest.request('POST', '/v1/perps/orders', payload=payload)
            order = parse_order(raw, state['entry_id'], '1')
            raw = await rest.request('GET', ORDER_PATH + state['entry_id'])
            order = parse_order(raw, state['entry_id'], '1')
            state['resting_confirmed'] = order['status'] == 'open' and Decimal(order['filled']) == 0
            state['phase'] = 'resting' if state['resting_confirmed'] else 'entry_terminal'
        except Exception as error:
            state.update(phase='entry_unknown', error_type=type(error).__name__)
        save(path, state)
        # 부모 프로세스가 이 작업 프로세스를 강제 종료한다. 정상 취소는 하지 않는다.
        while True:
            await asyncio.sleep(1)
    finally:
        client.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def worker(env_path, path):
    logging.disable(logging.CRITICAL)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            asyncio.run(worker_main(env_path, Path(path)))
        except Exception:
            pass


async def query_entry(rest, state):
    raw = await rest.request('GET', ORDER_PATH + state['entry_id'])
    return parse_order(raw, state['entry_id'], '1')


async def observe(rest, state, path, *, clock=time.monotonic, sleep=asyncio.sleep):
    start = state.get('killed_at_monotonic', clock())
    order = None
    while clock() - start <= 40:
        try:
            order = await query_entry(rest, state)
            if order['status'] in TERMINAL:
                break
        except Exception:
            pass
        await sleep(2)
    elapsed = clock() - start
    if order is None or order['status'] not in TERMINAL:
        state['fallback_cancel_used'] = True
        save(path, state)
        for _ in range(3):
            try:
                await rest.request('DELETE', ORDER_PATH + state['entry_id'])
            except Exception:
                pass
            try:
                order = await query_entry(rest, state)
                if order['status'] in TERMINAL:
                    break
            except Exception:
                pass
            await sleep(2)
    state.update(observed_cancel_after_sec=round(elapsed, 3),
                 deadman_verified=bool(order and order['status'] == 'canceled' and order['deadman_reason'] and
                     Decimal(order['filled']) == 0 and state.get('resting_confirmed') and
                     state.get('worker_terminated') and not state.get('fallback_cancel_used') and elapsed <= 30))
    save(path, state)
    if order is None or order['status'] not in TERMINAL:
        return {'resolved': False, 'phase': state['phase']}
    filled = Decimal(order['filled'])
    state.update(entry_status=order['status'], entry_filled=str(filled))
    if filled:
        current = await positions(rest)
        if current.get(MARKET, Decimal(0)) != filled or any(q for m,q in current.items() if m != MARKET):
            return {'resolved': False, 'phase': state['phase']}
        tick = Decimal(state['plan']['quote_increment'])
        if filled % Decimal(state['plan']['base_increment']):
            return {'resolved': False, 'phase': state['phase']}
        bid, _ = await quote()
        price = (bid * Decimal('.99') / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        if price <= 0:
            return {'resolved': False, 'phase': state['phase']}
        state.update(phase='close_pending', close_id=state['entry_id'] + '_close', close_size=str(filled))
        save(path, state)
        payload = {'market':MARKET,'side':'sell','type':'limit','timeInForce':'IOC', 'postOnly':False,
                   'reduceOnly':True,'size':str(filled),'price':str(price),'clientOrderId':state['close_id']}
        try:
            await rest.request('POST', '/v1/perps/orders', payload=payload)
        except Exception:
            pass
        for _ in range(15):
            try:
                raw = await rest.request('GET', ORDER_PATH + state['close_id'])
                closed = parse_order(raw, state['close_id'], filled, 'sell', cleanup=True)
                if closed['status'] in TERMINAL:
                    if Decimal(closed['filled']) != filled:
                        return {'resolved':False, 'phase':'close_pending'}
                    break
            except Exception:
                pass
            await sleep(2)
        else:
            return {'resolved':False, 'phase':'close_pending'}
    if any((await positions(rest)).values()):
        return {'resolved':False, 'phase':state['phase']}
    active = await rest.request('GET', '/v1/perps/orders', params={'status':'open', 'limit':1})
    if not isinstance(active, list) or active:
        return {'resolved':False, 'phase':state['phase']}
    state['phase'] = 'resolved'
    save(path, state)
    return {'resolved': True, 'deadman_verified': state['deadman_verified'], 'entry_status':order['status'],
            'entry_filled':str(filled), 'fallback_cancel_used':state.get('fallback_cancel_used',False),
            'observed_cancel_after_sec':state['observed_cancel_after_sec']}


async def run(args):
    cfg, secrets = load_config(args.config), load_secrets(args.env)
    if cfg.ondo.rest_url.rstrip('/') != BASE or cfg.ondo.ws_url != WS:
        raise ValueError('승인 대상 Ondo 메인넷 주소 불일치')
    path = path_for(cfg).resolve()
    if args.execute and (args.approve_max_entry_usd != '5' or not args.approve_deadman or not args.approve_cleanup):
        raise ValueError('5 USD 진입·계정 전체 데드맨·비상 취소/정리 승인 플래그가 필요하다')
    rest = make_rest(secrets)
    if path.exists():
        state = json.loads(path.read_text(encoding='utf-8'))
        if args.execute or state['scope_hash'] != scope(secrets):
            raise ValueError('기존 기록 보존 필요. 새 주문을 보내지 않는다')
        order = None
        try:
            order = await query_entry(rest, state)
        except Exception:
            pass
        return {'read_only':True,'phase':state['phase'],'order':order}
    plan = await preflight(rest)
    if not args.execute:
        return {'read_only':True,'plan':plan}
    if not all(plan[k] for k in ('sufficient_margin','no_positions','no_active_orders')):
        raise ValueError('증거금 또는 기존 포지션/주문 조건 미충족')
    state = {'phase':'prepared','entry_id':'olbot_dms_' + uuid.uuid4().hex,
             'scope_hash':scope(secrets),'plan':plan,'order_attempted':False}
    save(path, state, first=True)
    # 프로세스 간 REST 소유권을 넘길 때 마지막 요청과 최소 1초를 띄운다.
    await asyncio.sleep(1.05)
    process = multiprocessing.get_context('spawn').Process(target=worker, args=(str(Path(args.env).resolve()),str(path)), daemon=True)
    process.start()
    forced_termination = False
    try:
        deadline = time.monotonic() + 30
        while process.is_alive() and time.monotonic() < deadline:
            state = json.loads(path.read_text(encoding='utf-8'))
            if state['phase'] in {'resting','entry_terminal','entry_unknown'}:
                break
            await asyncio.sleep(.2)
    finally:
        killed_at = time.monotonic()
        if process.is_alive():
            process.terminate()
            forced_termination = True
        await asyncio.to_thread(process.join, 5)
    if process.is_alive():
        process.kill()
        forced_termination = True
        await asyncio.to_thread(process.join, 5)
        if process.is_alive():
            raise RuntimeError('시험 작업 프로세스 종료 확인 실패')
    state = json.loads(path.read_text(encoding='utf-8'))
    state['worker_terminated'] = forced_termination
    state['killed_at_monotonic'] = killed_at
    if not state['order_attempted']:
        state['phase'] = 'resolved'
        save(path, state)
        return {'resolved':True,'deadman_verified':False,'order_attempted':False}
    save(path, state)
    await asyncio.sleep(1.05)
    return await observe(rest, state, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--env', default='.env')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--approve-max-entry-usd')
    parser.add_argument('--approve-deadman', action='store_true')
    parser.add_argument('--approve-cleanup', action='store_true')
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            result = asyncio.run(run(args))
        except Exception as error:
            result = {'failed':True,'error_type':type(error).__name__}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('read_only') or (result.get('resolved') and result.get('deadman_verified')) else 1


if __name__ == '__main__':
    raise SystemExit(main())
