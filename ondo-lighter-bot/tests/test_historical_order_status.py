from decimal import Decimal as D
from urllib.parse import parse_qs,urlsplit
import pytest
from olbot.lighter.client import LighterTrader,OrderStatusError

def trader():
    t=LighterTrader('https://example.test',7,4,'dummy');t._auth_token='token';t._auth_token_ts=1e18
    return t

def order(**change):
    return dict({'client_order_index':17,'status':'filled','filled_base_amount':'453.0','initial_base_amount':'453.0'},**change)

async def test_older_than_primary_window_resolves_by_exact_id_on_history_second_page(monkeypatch):
    calls=[]
    async def get(url,headers=None):
        calls.append(url);assert headers=={'authorization':'token'}
        if '/accountOrders?' in url:return {'code':200,'orders':[]}
        q=parse_qs(urlsplit(url).query)
        if 'cursor' not in q:return {'code':200,'orders':[order(client_order_index=18)],'next_cursor':'x+/='}
        assert q['cursor']==['x+/='] and q['limit']==['100']
        return {'code':200,'orders':[order()]}
    monkeypatch.setattr('olbot.lighter.client.get_json',get)
    s=await trader().order_status(17)
    assert s.terminal and s.filled==D('453.0') and len(calls)==3

async def test_primary_found_does_not_query_history(monkeypatch):
    async def get(url,headers=None):
        assert '/accountOrders?' in url;return {'code':200,'orders':[order()]}
    monkeypatch.setattr('olbot.lighter.client.get_json',get)
    assert (await trader().order_status(17)).terminal

async def test_history_miss_is_bounded_rate_limited_and_never_rejection(monkeypatch):
    calls=[]
    async def get(url,headers=None):
        calls.append(url)
        if '/accountOrders?' in url:return {'code':200,'orders':[]}
        return {'code':200,'orders':[],'next_cursor':str(len(calls))}
    monkeypatch.setattr('olbot.lighter.client.get_json',get);t=trader()
    assert await t.order_status(17) is None and len(calls)==4
    assert await t.order_status(17) is None and len(calls)==5  # Recheck primary, not3 more history calls.

@pytest.mark.parametrize('rows',[[order(),order()],[order(status='open')],[{'client_order_index':17,'status':'filled'}]])
async def test_ambiguous_nonterminal_or_malformed_history_stays_unconfirmed(monkeypatch,rows):
    async def get(url,headers=None):return {'code':200,'orders':[] if '/accountOrders?' in url else rows}
    monkeypatch.setattr('olbot.lighter.client.get_json',get)
    with pytest.raises(OrderStatusError):await trader().order_status(17)

async def test_history_auth_error_is_error_not_unfilled_order(monkeypatch):
    async def get(url,headers=None):
        return {'code':200,'orders':[]} if '/accountOrders?' in url else {'code':401}
    monkeypatch.setattr('olbot.lighter.client.get_json',get)
    with pytest.raises(RuntimeError):await trader().order_status(17)
