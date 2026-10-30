import copy
from decimal import Decimal as D
import json
from pathlib import Path

import pytest

from olbot.paper import (Book, Scenario, common_step, parse_book, parse_trades, parse_ondo_funding,
                        parse_lighter_funding, reward_fraction, reward_scenario, vwap)
from olbot.paper_watch import Watch, write_report
from olbot.wsbase import ReconnectingWS

FIX = Path(__file__).parent/'fixtures/paper-public'


def raw(name): return json.loads((FIX/name).read_text(encoding='utf-8'))


def scenario(model='touch'):
    return Scenario('BTC',5,model,D('.01'),D('.01'),D(10),D('.01'))


def book(ts=5):
    b=Book();b.apply(True,1,None,[[(D('99.9'),D(100))],[(D('100.1'),D(100))]],ts)
    return b


def test_real_orderbook_snapshot_delta_and_gap_recovery():
    initial=parse_book(raw('lighter-subscribed-order_book-order_book-0.json'))
    delta=parse_book(raw('lighter-update-order_book-order_book-0.json'))
    b=Book();b.apply(*initial[1:],100)
    assert b.mid>0
    assert delta[3]==initial[2]
    b.apply(*delta[1:],100.05)
    assert b.mid>0
    with pytest.raises(ValueError,match='nonce'):
        b.apply(False,999,-1,[[],[]],101)
    assert b.mid is None
    b.apply(*initial[1:],102)
    assert b.mid>0


def test_actual_trade_and_funding_parsers_have_correct_units():
    trades=parse_trades(raw('ondo-update-tradesPerps.json'))
    assert trades and trades[0][2] in ('buy','sell') and trades[0][3]>0 and trades[0][5]>1e9
    rates=dict(parse_ondo_funding(raw('ondo-update-fundingRatesPerps.json')))
    assert rates['BTC']==D('-.0000431')
    mid,rate=parse_lighter_funding(raw('lighter-subscribed-market_stats-market_stats-0.json'))
    assert mid==0 and rate==D('.000012')  # 0.0012 percent


def test_new_public_clients_inherit_reconnecting_and_never_login(tmp_path):
    w=Watch(tmp_path,'unused',100)
    w.meta={'BTC':{'market':'BTC-USD.P'},'ETH':{'market':'ETH-USD.P'}}
    w.base_by_id={1:'BTC',0:'ETH'}
    for client in (w.ondo_ws,w.lighter_ws):
        assert isinstance(client,ReconnectingWS)
        for sub in client.subscriptions():
            assert 'auth' not in sub and 'login' not in json.dumps(sub)
    assert all(sub['numPastTrades']==0 for sub in w.ondo_ws.subscriptions() if sub['channel']=='tradesPerps')


def test_common_step_and_depth_vwap_not_last_price_fallback():
    assert common_step(D('.03'),D('.02'))==D('.06')
    assert vwap([(D(100),D(1)),(D(101),D(1))],D(2))==D('100.5')
    assert vwap([(D(100),D(1))],D(2)) is None


def test_post_only_quote_rounds_inside_reward_boundary_and_has_two_second_gap():
    s=scenario();s.quote(D(100),D('99.99'),D('100.01'),1)
    assert s.orders['buy'].price==D('99.95') and s.orders['sell'].price==D('100.05')
    assert s.trade('sell',D('99.95'),D(1),2.99,3,D(100),True) is None
    assert s.trade('sell',D('99.95'),D(1),3.1,3.1,D(100),True)


def test_touch_and_through_are_alternatives_not_guaranteed_fill_probabilities():
    a,b=scenario('touch'),scenario('through')
    for s in (a,b): s.quote(D(100),D('99.99'),D('100.01'),1)
    assert a.trade('sell',D('99.95'),D(1),4,4,D(100),True)
    assert b.trade('sell',D('99.95'),D(1),4,4,D(100),True) is None
    assert b.trade('sell',D('99.94'),D(1),4,4,D(100),True)


def test_partial_fills_accumulate_to_minimum_and_hedge_only_after_300ms():
    s=scenario();s.quote(D(100),D('99.99'),D('100.01'),1)
    b=book(4)
    s.trade('sell',D('99.95'),D('.05'),4,4,D(100),True)
    s.advance(4,D(100),b,True)
    assert s.pending is None  # 약 5 USD, 최소 주문 미만
    s.trade('sell',D('99.95'),D('.1'),4.1,4.1,D(100),True)
    s.advance(4.1,D(100),b,True)
    assert s.pending and s.pending['signed']==D('.15')
    b.ts=4.399;s.advance(4.399,D(100),b,True)
    assert s.hedges==0
    b.ts=4.401;result=s.advance(4.401,D(100),b,True)
    assert s.hedges==1 and s.pos+s.lpos==0 and s.fills==2
    assert result['execution']=='99.9'


@pytest.mark.parametrize('direction',[1,-1])
def test_basis_delay_spread_signs_and_cash_include_fees_exactly_once(direction):
    s=scenario();s.quote(D(100),D('99.99'),D('100.01'),1)
    aggressor='sell' if direction==1 else 'buy'
    price=D('99.95') if direction==1 else D('100.05')
    s.trade(aggressor,price,D('.5'),4,4,D(100),True)
    b=book(4);s.advance(4,D(100),b,True,D(0),D(0))
    b.apply(True,2,None,[[(D('100.9'),D(100))],[(D('101.1'),D(100))]],4.301)
    s.advance(4.301,D(100),b,True,D(0),D(0))
    assert s.delay_cost==D(direction)*D('.5')*(D(100)-D(101))
    assert s.spread_cost==D('.05')
    row=s.result(D(100),b,4.4)
    expected=s.cash+s.pos*100+s.lpos*101
    assert D(row['mark_pnl_ex_reward_funding_usd'])==expected
    assert s.maker_fees==price*D('.5')*D('.0001')


def test_unknown_hedge_cost_is_not_zero_or_retransmitted_and_failure_counted_once():
    s=scenario();s.pos=D(1);s.unmatched_value=s.unmatched_ref=D(100)
    b=book(4);s.advance(4,D(100),b,True)
    for now in (7,8,9): s.advance(now,D(100),b,False)
    assert s.failed_hedges==1 and s.pending and s.hedges==0 and s.killed
    assert s.result(D(100),b,9)['hedge_cost_per_fill_usd'] is None


def test_rewards_have_three_distinct_buckets_and_boundary_uncertainty():
    assert [reward_fraction(D(x)) for x in ('5','10','18')]==[D('.85'),D('.10'),D('.05')]
    assert reward_fraction(D('5.5')) is None
    sample={'bids':[['99.99','1'],['99.945','100'],['99.82','100']],
            'asks':[['100.01','1'],['100.055','100'],['100.18','100']]}
    low,high,ambiguous=reward_scenario(sample,[(D('99.945'),D(1))],D(5000))
    assert ambiguous and low<high and low>=0
    truncated={'bids':[['99.99','1']]*100,'asks':[['100.01','1']]*100}
    assert reward_scenario(truncated,[(D('99.95'),D(1))],D(5000)) is None


def test_duplicate_trades_not_counted_twice_and_stale_replay_not_filled(tmp_path):
    w=Watch(tmp_path,'unused',100)
    s=scenario();s.quote(D(100),D('99.99'),D('100.01'),1)
    w.scenarios=[s];w.healthy=lambda base,now:True
    w.board.update_ondo('BTC',D('99.99'),D('100.01'),4)
    trade=('BTC','real-id','sell',D('99.95'),D('.1'),4)
    w.on_trade(trade,4);w.on_trade(trade,4)
    assert s.fills==1
    w.on_trade(('BTC','late','sell',D('99.95'),D('.1'),1),4)
    assert s.fills==1


def test_report_has_six_conditions_and_keeps_missing_cost_as_unknown(tmp_path):
    w=Watch(tmp_path,'unused',100)
    for base in ('BTC','ETH'):
        w.board.update_ondo(base,D('99.99'),D('100.01'),4)
        w.books[base]=book(4)
        for distance in (5,10,18):
            for model in ('touch','through'):
                w.scenarios.append(Scenario(base,distance,model,D('.01'),D('.01'),D(10),D('.01')))
    w.write('summary.json',w.summary(5))
    write_report(tmp_path)
    report=(tmp_path/'REPORT.md').read_text(encoding='utf-8')
    compact=report.split('아래는 모형별')[0]
    assert compact.count('| BTC |')==3 and compact.count('| ETH |')==3
    assert '산출 불가' in compact
    data=json.loads((tmp_path/'report-data.json').read_text(encoding='utf-8'))
    assert all(r['hedge_cost_per_fill_usd'] is None and r['net_week_low_usdc'] is None for r in data['rows'])
