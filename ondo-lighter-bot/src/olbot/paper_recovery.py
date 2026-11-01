"""Recover only fully reconciled, hedged virtual inventories; never invent gap fills."""
from decimal import Decimal as D
import json
from pathlib import Path


def lines(folder,name):
    p=Path(folder)/name
    return [json.loads(x) for x in p.read_text(encoding='utf-8').splitlines() if x.strip()] if p.exists() else []


def reconciled_state(folder,summary):
    fills=lines(folder,'fills.jsonl');hedges=lines(folder,'hedges.jsonl');out={}
    keys={(r['base'],r['distance_bps'],r['model']) for r in summary['rows']}
    if any((r['base'],r['distance'],r['model']) not in keys for r in fills+hedges):raise ValueError('unknown virtual model in journal')
    for r in summary['rows']:
        key=(r['base'],r['distance_bps'],r['model'])
        fs=[x for x in fills if (x['base'],x['distance'],x['model'])==key]
        hs=[x for x in hedges if (x['base'],x['distance'],x['model'])==key]
        if r['pending_hedge'] or len(fs)!=r['fills'] or len(hs)!=r['hedges'] or r['costed_fills']!=len(fs):
            raise ValueError('virtual journal/checkpoint not fully confirmed')
        if any(x['recv_ts']>summary['updated_ts'] for x in fs) or any(x['ts']>summary['updated_ts'] for x in hs):
            raise ValueError('virtual event after saved summary')
        signed=lambda x:D(x['size'])*(1 if x['side']=='buy' else -1)
        op=sum((signed(x) for x in fs),D(0));lp=-sum((D(x['signed_size']) for x in hs),D(0))
        fees=sum((D(x['maker_fee']) for x in fs),D(0))
        notional=sum((abs(signed(x)*D(x['price'])) for x in fs),D(0))
        if op+lp!=0 or op!=D(r['position']) or lp!=D(r['lighter_position']) or fees!=D(r['maker_fees_usd']) or notional!=D(r['fill_notional_usd']):
            raise ValueError('virtual positions/fees do not reconcile')
        cash=-sum((signed(x)*D(x['price'])+D(x['maker_fee']) for x in fs),D(0))+sum((D(x['signed_size'])*D(x['execution']) for x in hs),D(0))
        for field,logged in [('basis_cost_usd','basis_usd'),('delay_cost_usd','delay_usd'),('spread_depth_cost_usd','spread_usd')]:
            if sum((D(x[logged]) for x in hs),D(0))!=D(r[field]):raise ValueError('virtual cost totals do not reconcile')
        out[key]={'pos':op,'lpos':lp,'cash':cash,'maker_fees':fees,'fill_notional':notional,
                  'fills':len(fs),'hedges':len(hs),'costed_fills':len(fs),'costed_fees':fees,
                  'basis_cost':D(r['basis_cost_usd']),'accumulation_cost':D(r['accumulation_cost_usd']),
                  'delay_cost':D(r['delay_cost_usd']),'spread_cost':D(r['spread_depth_cost_usd']),
                  'delayed_seconds':[x['delay_sec'] for x in hs],
                  'funding_estimate':D(r['funding_estimate_usd']),
                  'funding_unknown_seconds':r['funding_unknown_seconds']}
    return out
