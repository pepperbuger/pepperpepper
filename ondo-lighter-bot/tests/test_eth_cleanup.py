import json
from decimal import Decimal as D
from unittest.mock import AsyncMock
import pytest
from olbot.eth_cleanup import execute,make_eth_leg
from olbot.eth_cleanup import EthNative
from types import SimpleNamespace


class Fake:
    def __init__(self,path,unknown=False,partial=False):
        self.path=path;self.pos={'ondo':D('.01'),'lighter':D('-.01')};self.sent=[];self.unknown=unknown;self.partial=partial
    async def snapshot(self):
        return {'ondo_positions':{'ETH':str(self.pos['ondo'])},'lighter_positions':{'ETH':str(self.pos['lighter'])},'ondo_open':0,'lighter_open':0,'pending':False}
    async def leg(self,base,venue,position):
        assert base=='ETH'
        return make_eth_leg(venue,position,[('2200','1')],[('2201','1')],D(1),D('.001'),D('.001'),D(0))
    def order_id(self,venue):return 'test-'+venue
    async def send(self,leg,cid):
        saved=json.loads(self.path.read_text(encoding='utf-8'))
        assert saved['attempts'][-1]['client_id']==cid and saved['attempts'][-1]['phase']=='send_unknown'
        self.sent.append(leg['venue'])
        if self.unknown:raise TimeoutError()
    async def confirm(self,leg,cid,before):
        if self.unknown:return None
        size=D(leg['size'])/(2 if self.partial else 1)
        self.pos[leg['venue']]+=size if leg['side']=='buy' else -size
        return {'terminal':True,'status':'filled' if not self.partial else 'canceled','filled':str(size),'quote':str(size*D(leg['expected_vwap'])),'fee':'0'}


async def test_cleanup_requires_specific_approval(tmp_path):
    f=Fake(tmp_path/'j.json')
    with pytest.raises(ValueError):await execute(f,f.path)
    assert not f.sent and not f.path.exists()


async def test_eth_cleanup_two_durable_sends_then_final_flat_checks_no_rerun(tmp_path,monkeypatch):
    monkeypatch.setattr('olbot.eth_cleanup.asyncio.sleep',AsyncMock())
    f=Fake(tmp_path/'j.json');r=await execute(f,f.path,approved=True)
    assert r['phase']=='resolved' and r['orders_sent']==2 and f.sent==['ondo','lighter']
    assert all(q==0 for q in f.pos.values())
    with pytest.raises(ValueError):await execute(f,f.path,approved=True)


@pytest.mark.parametrize('kind',['unknown','partial'])
async def test_unknown_or_partial_first_leg_stops_without_second_send(tmp_path,kind):
    f=Fake(tmp_path/'j.json',unknown=kind=='unknown',partial=kind=='partial')
    r=await execute(f,f.path,approved=True)
    assert r['phase']=='halted' and r['urgent'] and r['orders_sent']==1 and f.sent==['ondo']


async def test_unmanaged_position_blocks_cleanup_before_send(tmp_path):
    f=Fake(tmp_path/'j.json');original=f.snapshot
    async def snapshot():
        s=await original();s['ondo_positions']['LIT']='1';return s
    f.snapshot=snapshot;r=await execute(f,f.path,approved=True)
    assert r['phase']=='halted' and not f.sent


def test_eth_cleanup_protects_price_depth_and100usd_ceiling():
    leg=make_eth_leg('ondo',D('.01'),[('2200','1')],[('2201','1')],D(1),D('.001'),D('.001'),D(0))
    assert D(leg['price_limit'])>=D(2200)*D('.998') and leg['side']=='sell'
    with pytest.raises(ValueError):make_eth_leg('lighter',D('.05'),[('2200','1')],[('2201','1')],D(1),D('.001'),D('.001'),D(0))
    with pytest.raises(ValueError):make_eth_leg('ondo',D('.01'),[('2200','.001'),('2100','1')],[('2201','1')],D(1),D('.001'),D('.001'),D(0))


@pytest.mark.parametrize('limits',[
    {'code':200,'user_tier':'premium','current_maker_fee_tick':40,'current_taker_fee_tick':280},
    {'code':200,'user_tier':'standard','current_maker_fee_tick':0,'current_taker_fee_tick':1},
    {'code':200,'user_tier':'standard','current_taker_fee_tick':0},
])
async def test_eth_native_blocks_nonzero_or_missing_standard_fees_before_ondo(limits,monkeypatch):
    for module in ('trial','ondo_trial','paired_trial'):
        monkeypatch.setattr('olbot.eth_cleanup.'+module+'.require_resolved_trial',lambda cfg:None)
    monkeypatch.setattr('olbot.eth_cleanup.trial.get',AsyncMock(return_value=limits))
    native=EthNative.__new__(EthNative);native.cfg=object()
    native.trader=SimpleNamespace(connect=AsyncMock(),account_index=7,_auth=lambda:'fake-test-token')
    native.rest=SimpleNamespace(request=AsyncMock())
    with pytest.raises(ValueError,match='Standard'):await native.setup()
    native.rest.request.assert_not_called()


def test_eth_measurement_config_has_only_eth_and_eligibility_not_dollar_forecast():
    from pathlib import Path
    from olbot.config import load_config
    from olbot.rewards import policy_for_config,load_policy
    cfg=load_config(Path(__file__).parent.parent/'config.measure-eth.example.yaml')
    assert cfg.bases==['ETH'] and cfg.risk.daily_loss_limit_usd==D(2)
    policy=policy_for_config(cfg)
    assert policy.allowed_bases==frozenset({'ETH'}) and policy.per_market_usdc is None
    with pytest.raises(ValueError):load_policy(cfg.rewards.policy_path)
