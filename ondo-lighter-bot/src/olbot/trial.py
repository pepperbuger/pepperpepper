"""Lighter 주문 조회 검증용 단발 시험. 기본은 조회만, 전송은 --execute 필요.

진입은 최대 12 USD, IOC 1회. 체결 시 reduce-only 정리 1회만 허용한다.
미확정이면 재전송하지 않고 영속 기록을 남긴다. 실제 실행에는 별도 사람 승인이 필요하다.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import time
import urllib.request

from olbot.config import load_config, load_secrets
from olbot.lighter.client import LighterTrader, parse_order_status

BASE = 'https://mainnet.zklighter.elliot.ai'
ENTRY_CAP = Decimal('12')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _get(path, token=None):
    headers = {'authorization': token} if token else {}
    request = urllib.request.Request(BASE + path, headers=headers, method='GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=15) as response:
        result = json.load(response)
    if result.get('code') != 200:
        raise ValueError('Lighter 조회 거절')
    return result


async def get(path, token=None):
    return await asyncio.to_thread(_get, path, token)


def journal_path(cfg):
    return Path(cfg.storage.sqlite_path).parent / 'lighter-order-trial.json'


def require_resolved_trial(cfg):
    path = journal_path(cfg)
    if path.exists() and json.loads(path.read_text(encoding='utf-8')).get('phase') != 'resolved':
        raise RuntimeError('단발 시험 주문이 미확정이다. lighter-order-trial.json을 보존하고 주문부터 확인해야 한다')


def save(path, state, *, first=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    destination = path if first else path.with_suffix('.tmp')
    with destination.open('x' if first else 'w', encoding='utf-8') as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    if not first:
        os.replace(destination, path)


def signed_positions(account):
    rows = account['positions']
    if not isinstance(rows, list):
        raise ValueError('positions 형식 오류')
    result = {}
    for row in rows:
        market = int(row['market_id'])
        quantity = Decimal(str(row['position']))
        sign = int(row['sign'])
        if not quantity.is_finite() or quantity < 0 or (quantity and sign not in (-1, 1)) or market in result:
            raise ValueError('포지션 응답 오류')
        result[market] = quantity * sign
    return result


def calculate_plan(meta, bids, asks):
    bid, ask = Decimal(bids), Decimal(asks)
    if not (bid.is_finite() and ask.is_finite() and 0 < bid < ask):
        raise ValueError('호가 응답 오류')
    price_scale = Decimal(10) ** int(meta['supported_price_decimals'])
    size_scale = Decimal(10) ** int(meta['supported_size_decimals'])
    price = (bid * Decimal('.99') * price_scale).to_integral_value(rounding=ROUND_DOWN) / price_scale
    minimum = Decimal(meta['min_quote_amount'])
    min_base = Decimal(meta['min_base_amount'])
    if price <= 0 or not minimum.is_finite() or not min_base.is_finite() or minimum <= 0 or min_base <= 0:
        raise ValueError('최소 주문 응답 오류')
    size = (max(min_base, minimum * Decimal('1.01') / price) * size_scale).to_integral_value(rounding=ROUND_CEILING) / size_scale
    if size * ask > ENTRY_CAP:
        raise ValueError('최소 주문이 승인 범위 12 USD를 초과한다')
    return {'market_id': int(meta['market_id']), 'size': str(size), 'price': str(price),
            'base_amount': int(size * size_scale), 'price_int': int(price * price_scale),
            'reference_notional_usd': str(size * ask), 'size_decimals': int(meta['supported_size_decimals']),
            'price_decimals': int(meta['supported_price_decimals'])}


async def plan_for(trader):
    books = (await get('/api/v1/orderBooks'))['order_books']
    markets = [m for m in books if m['symbol'] == 'LIT' and m['market_type'] == 'perp' and m['status'] == 'active']
    if len(markets) != 1:
        raise ValueError('LIT 마켓 식별 실패')
    market_id = int(markets[0]['market_id'])
    meta = (await get(f'/api/v1/orderBookDetails?market_id={market_id}'))['order_book_details'][0]
    if int(meta['market_id']) != market_id:
        raise ValueError('마켓 불일치')
    book = await get(f'/api/v1/orderBookOrders?market_id={market_id}&limit=10')
    bid = max(Decimal(r['price']) for r in book['bids'] if Decimal(r['remaining_base_amount']) > 0)
    ask = min(Decimal(r['price']) for r in book['asks'] if Decimal(r['remaining_base_amount']) > 0)
    plan = calculate_plan(meta, bid, ask)
    account = await trader.account()
    if int(account['account_index']) != trader.account_index:
        raise ValueError('계정 응답 불일치')
    positions = signed_positions(account)
    plan['no_existing_positions'] = not any(positions.values())
    plan['sufficient_margin'] = Decimal(str(account['available_balance'])) >= Decimal('15')
    return plan


async def final_order(trader, plan, coi, is_sell, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = await get(f'/api/v1/accountOrders?account_index={trader.account_index}&client_order_indexes={coi}', trader._auth())
            orders = [r for r in data['orders'] if int(r['client_order_index']) == coi]
            if len(orders) != 1:
                await asyncio.sleep(2)
                continue
            raw = orders[0]
            if (int(raw['owner_account_index']) != trader.account_index or
                    int(raw['market_index']) != plan['market_id'] or
                    type(raw['is_ask']) is not bool or raw['is_ask'] != is_sell):
                raise ValueError('주문 식별 불일치')
            order = parse_order_status(raw)
            if order.initial != Decimal(plan['size']):
                raise ValueError('주문 수량 불일치')
            if order.terminal:
                # 진입 전 0, 정리 전은 확인된 매수 체결량이다.
                before = Decimal(plan.get('position_before', '0'))
                expected = before + (-order.filled if is_sell else order.filled)
                positions = signed_positions(await trader.account())
                if positions.get(plan['market_id'], Decimal(0)) == expected:
                    return order
        except Exception:
            # 응답 오류/반영 지연/통신 오류는 전부 대기. 다시 주문하지 않는다.
            pass
        await asyncio.sleep(2)
    return None


async def execute(trader, plan, path):
    if not plan['no_existing_positions'] or not plan['sufficient_margin']:
        raise ValueError('기존 포지션 또는 증거금 부족으로 시험 거부')
    active = await get(f'/api/v1/accountActiveOrders?account_index={trader.account_index}', trader._auth())
    if active['orders']:
        raise ValueError('기존 주문이 있어 시험 거부')
    coi = trader.new_client_order_index()
    state = {'phase': 'entry_pending', 'entry_id': coi, 'plan': plan,
             'scope_hash': hashlib.sha256(f'{BASE}:{trader.account_index}'.encode()).hexdigest()}
    save(path, state, first=True)  # 중복 실행 거부 및 전송 전 fsync
    try:
        # 응답을 잃어도 이 번호만 조회한다. SDK의 오류 문구로 재전송하지 않는다.
        try:
            await trader.client.create_market_order(market_index=plan['market_id'], client_order_index=coi,
                base_amount=plan['base_amount'], avg_execution_price=plan['price_int'], is_ask=False)
        except Exception:
            pass
        order = await final_order(trader, plan, coi, False)
        if order is None:
            return {'phase': state['phase'], 'resolved': False}
        state['entry_status'], state['entry_filled'] = order.status, str(order.filled)
        if order.filled:
            # 확정된 체결 수량만 reduce-only로 한 번 정리한다.
            book = await get(f"/api/v1/orderBookOrders?market_id={plan['market_id']}&limit=10")
            bid = max(Decimal(r['price']) for r in book['bids'] if Decimal(r['remaining_base_amount']) > 0)
            scale = Decimal(10) ** plan['price_decimals']
            price_int = int((bid * Decimal('.995') * scale).to_integral_value(rounding=ROUND_DOWN))
            size_int = order.filled * (Decimal(10) ** plan['size_decimals'])
            if size_int != size_int.to_integral_value() or price_int <= 0:
                raise ValueError('정리 수량/가격 형식 오류')
            close_plan = {**plan, 'size': str(order.filled), 'position_before': str(order.filled)}
            close_id = trader.new_client_order_index()
            state.update(phase='close_pending', close_id=close_id, close_plan=close_plan)
            save(path, state)
            try:
                await trader.client.create_market_order(market_index=plan['market_id'], client_order_index=close_id,
                    base_amount=int(size_int), avg_execution_price=price_int, is_ask=True, reduce_only=True)
            except Exception:
                pass
            closed = await final_order(trader, close_plan, close_id, True)
            if closed is None or closed.filled != order.filled:
                return {'phase': state['phase'], 'resolved': False}
            state['close_status'] = closed.status
        state['phase'] = 'resolved'
        save(path, state)
        return {'phase': 'resolved', 'resolved': True, 'entry_status': order.status, 'entry_filled': str(order.filled)}
    except Exception:
        # 실패해도 기록을 보존하고 live 재개를 차단한다.
        return {'phase': state['phase'], 'resolved': False}


async def run(args):
    cfg, secrets = load_config(args.config), load_secrets(args.env)
    if cfg.lighter.base_url.rstrip('/') != BASE:
        raise ValueError('이 시험은 승인 대상 메인넷 설정에서만 사용한다')
    path = journal_path(cfg)
    if args.execute and (args.approve_max_entry_usd != '12' or not args.approve_cleanup):
        raise ValueError('최대 12 USD 진입과 reduce-only 정리 승인 플래그가 필요하다')
    if args.execute and path.exists():
        raise ValueError('기존 시험 기록이 있다. 삭제하거나 새 주문을 보내지 말고 먼저 조회해야 한다')
    trader = LighterTrader(BASE, int(secrets.lighter_account_index), int(secrets.lighter_api_key_index), secrets.lighter_api_key_private_key)
    try:
        await trader.connect()
        if path.exists():
            state = json.loads(path.read_text(encoding='utf-8'))
            scope = hashlib.sha256(f'{BASE}:{trader.account_index}'.encode()).hexdigest()
            if state['scope_hash'] != scope:
                raise ValueError('시험 계정 불일치')
            if state['phase'] == 'resolved':
                return {'read_only': True, 'phase': 'resolved'}
            closing = state['phase'] == 'close_pending'
            plan = state['close_plan'] if closing else state['plan']
            coi = state['close_id'] if closing else state['entry_id']
            order = await final_order(trader, plan, coi, closing)
            resolved = order is not None and (
                order.filled == Decimal(state['entry_filled']) if closing else order.filled == 0)
            if resolved:
                state['phase'] = 'resolved'
                save(path, state)
            return {'read_only': True, 'phase': state['phase'], 'resolved': resolved}
        plan = await plan_for(trader)
        if not args.execute:
            return {'read_only': True, 'plan': plan}
        return await execute(trader, plan, path)
    finally:
        await trader.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--env', default='.env')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--approve-max-entry-usd', default=None)
    parser.add_argument('--approve-cleanup', action='store_true')
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            result = asyncio.run(run(args))
        except Exception as error:
            result = {'failed': True, 'error_type': type(error).__name__}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('read_only') or result.get('resolved') else 1


if __name__ == '__main__':
    raise SystemExit(main())
