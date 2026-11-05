import json
from decimal import Decimal as D
from unittest.mock import AsyncMock
import pytest
from olbot.btc_cleanup import execute,make_btc_leg


class Fake:
    def __init__(self,path,unknown=False,partial=False):
        self.path=path;self.pos={'ondo':D('.0003'),'lighter':D('-.0003')};self.sent=[];self.unknown=unknown;self.partial=partial
    async def snapshot(self):
        return {'ondo_positions':{'BTC':str(self.pos['ondo'])},'lighter_positions':{'BTC':str(self.pos['lighter'])},'ondo_open':0,'lighter_open':0,'pending':False}
    async def leg(self,base,venue,position):
        assert base=='BTC'
        return make_btc_leg(venue,position,[('70000','1')],[('70001','1')],D(1),D('.0001'),D('.0001'),D(0))
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


async def test_btc_cleanup_two_durable_sends_then_final_flat_checks_no_rerun(tmp_path,monkeypatch):
    monkeypatch.setattr('olbot.btc_cleanup.asyncio.sleep',AsyncMock())
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


def test_btc_cleanup_protects_price_depth_and100usd_ceiling():
    leg=make_btc_leg('ondo',D('.0003'),[('70000','1')],[('70001','1')],D(1),D('.0001'),D('.0001'),D(0))
    assert D(leg['price_limit'])>=D(70000)*D('.998') and leg['side']=='sell'
    with pytest.raises(ValueError):make_btc_leg('lighter',D('.002'),[('70000','1')],[('70001','1')],D(1),D('.0001'),D('.0001'),D(0))
    with pytest.raises(ValueError):make_btc_leg('ondo',D('.0003'),[('70000','.0001'),('69000','1')],[('70001','1')],D(1),D('.0001'),D('.0001'),D(0))
