"""확정 펀딩율과 가상 재고를 결합한 보고. 예상 펀딩을 두 번 더하지 않는다."""
from __future__ import annotations
from decimal import Decimal as D
import gzip
import json
from pathlib import Path

from olbot.funding import summarize,paired_fee_credit


def load_lines(path):
    path=Path(path)
    if not path.exists(): return []
    with path.open(encoding='utf-8') as stream: return [json.loads(x) for x in stream if x.strip()]


def price_proxies(comparison):
    out={'BTC':[],'ETH':[]}
    with gzip.open(Path(comparison)/'reward-inputs.jsonl.gz','rt',encoding='utf-8') as stream:
        for line in stream:
            r=json.loads(line)
            b=r['book'];mid=(D(b['bids'][0][0])+D(b['asks'][0][0]))/2
            out[r['base']].append((r['ts'],mid))
    return out


def before_price(prices,hour):
    candidates=[(t,p) for t,p in prices if 0<hour-t<=90]
    return max(candidates,key=lambda x:x[0])[1] if candidates else None


def scenario_funding(comparison,hourly,base,distance,model,cutoff,prices=None):
    events=[]
    for r in load_lines(Path(comparison)/'fills.jsonl'):
        if (r['base'],r['distance'],r['model'])==(base,distance,model):
            events.append((r['recv_ts'],'ondo',D(r['size'])*(1 if r['side']=='buy' else -1)))
    for r in load_lines(Path(comparison)/'hedges.jsonl'):
        if (r['base'],r['distance'],r['model'])==(base,distance,model):
            events.append((r['ts'],'lighter',-D(r['signed_size'])))
    events.sort(key=lambda x:x[0])
    prices=price_proxies(comparison)[base] if prices is None else prices[base]
    separated={v:[r for r in events if r[1]==v] for v in ('ondo','lighter')}
    cursors={'ondo':0,'lighter':0};positions={'ondo':D(0),'lighter':D(0)}
    total=D(0);unknown=[];known=[]
    for row in sorted((r for r in hourly if r['base']==base and r['hour']<=cutoff),key=lambda r:r['hour']):
        for venue in ('ondo','lighter'):
            ts=row.get(venue+'_settled_ts') or row['hour']
            rows=separated[venue]
            while cursors[venue]<len(rows) and rows[cursors[venue]][0]<ts:
                positions[venue]+=rows[cursors[venue]][2];cursors[venue]+=1
        op,lp=positions['ondo'],positions['lighter']
        credit=paired_fee_credit(op,lp,before_price(prices,row.get('ondo_settled_ts') or row['hour']),
            D(row['ondo_rate']) if row['ondo_rate'] is not None else None,
            D(row['lighter_per_base_value']) if row['lighter_per_base_value'] is not None else None)
        detail={'hour':row['hour'],'ondo_position':str(op),'lighter_position':str(lp),
                'credit_usdc':str(credit) if credit is not None else None,'rate_missing':row['missing']}
        if credit is None: unknown.append(detail)
        else: total+=credit;known.append(detail)
    return {'known_credit_subtotal_usdc':str(total),'credit_usdc':str(total) if not unknown else None,
            'unknown_hours':unknown,'known_hours':known,'note':'Ondo oracle is proxied by preceding public mid <=90sec; Lighter uses signed per-base historical funding value'}


def hold_result(base,rows,stats,entry,exit_,prices):
    direction=1 if stats['fixed_direction']=='ondo_short_lighter_long' else (-1 if stats['fixed_direction']=='ondo_long_lighter_short' else None)
    if not direction or not entry: return {'gross_usdc':None,'reason':'no pre-window direction signal or cost proxy'}
    q=D(entry['quantity']);gross=D(0);missing=[]
    for row in rows:
        op=-D(direction)*q;lp=D(direction)*q
        credit=paired_fee_credit(op,lp,before_price(prices,row.get('ondo_settled_ts') or row['hour']),
            D(row['ondo_rate']) if row['ondo_rate'] is not None else None,
            D(row['lighter_per_base_value']) if row['lighter_per_base_value'] is not None else None)
        if credit is None: missing.append(row['hour'])
        else: gross+=credit
    entry_cost=entry[str(direction)]['entry_cost']
    exit_cost=exit_[str(direction)]['exit_cost'] if exit_ else None
    fee_net=gross-D(entry_cost)-D(exit_cost) if not missing and entry_cost is not None and exit_cost is not None else None
    basis_pnl=(-D(direction)*q*((D(exit_['ondo_mid'])-D(exit_['lighter_mid']))-(D(entry['ondo_mid'])-D(entry['lighter_mid'])))) if exit_ else None
    return {'quantity':str(q),'direction':stats['fixed_direction'],'funding_known_subtotal_usdc':str(gross),
            'gross_usdc':str(gross) if not missing else None,'missing_hours':missing,'entry_cost':entry_cost,'exit_cost':exit_cost,
            'funding_less_entry_exit_usdc':str(fee_net) if fee_net is not None else None,
            'basis_mark_pnl_usdc':str(basis_pnl) if basis_pnl is not None else None,
            'total_with_basis_usdc':str(fee_net+basis_pnl) if fee_net is not None and basis_pnl is not None else None,
            'entry_proxy_ts':entry['ts'],'exit_proxy_ts':exit_['ts'] if exit_ else None,
            'note':'initial per-leg notional <=200USD, fixed common base quantity. Cost proxies omit unobserved order latency; neither exact oracle cashflow nor actual return.'}


def number(value): return f'{D(value):.6f}' if value is not None else '누락/산출 불가'


def write_funding_report(comparison,folder,paper_data=None):
    comparison,folder=Path(comparison),Path(folder)
    data=json.loads((folder/'hourly-latest.json').read_text(encoding='utf-8'))
    entries=json.loads((folder/'entry-cost-proxies.json').read_text(encoding='utf-8'))
    exits={r['base']:r for r in load_lines(folder/'exit-cost-proxies.jsonl')}
    prices=price_proxies(comparison)
    stats={};holds={}
    for base in ('BTC','ETH'):
        rows=[r for r in data['rows'] if r['base']==base]
        seed=data['seeds'].get(base)
        stats[base]=summarize(rows,seed['difference'] if seed else None)
        exit_=exits.get(base)
        if exit_ and data['updated_ts']-exit_['ts']>5400: exit_=None
        holds[base]=hold_result(base,rows,stats[base],entries.get(base),exit_,prices[base])
    result={'updated_ts':data['updated_ts'],'statistics':stats,'fixed_quantity_holds':holds,'rows':data['rows'],
            'orders_sent':0,'credentials_read':False}
    lines=['# 시간당 확정 펀딩 비교', '',
           '모든 비율은 시간당 소수 비율로 정규화했다. 평균/중앙값은 두 거래소 모두 있는 시간만 사용하며 누락은 0으로 채우지 않는다.', '',
           '| 마켓 | 일치/기대 시간 | 평균 차이 bps/h | 중앙값 bps/h | 우세 부호 비율 | 최장 +/− 연속 시간 |', '|---|---|---:|---:|---|---|']
    for b,s in stats.items():
        mean=str(D(s['mean_hourly_difference'])*10000) if s['mean_hourly_difference'] is not None else None
        median=str(D(s['median_hourly_difference'])*10000) if s['median_hourly_difference'] is not None else None
        share=f"{s['dominant_sign']} {D(s['dominant_sign_fraction'])*100:.1f}%" if s['dominant_sign_fraction'] is not None else '누락'
        lines.append(f"| {b} | {s['hours_paired']}/{s['hours_expected']} | {number(mean)} | {number(median)} | {share} | {s['longest_positive_hourly_streak']}/{s['longest_negative_hourly_streak']} |")
    lines+=['', '## 초기 명목 200USD 이내인 동일 수량 쌍', '',
            '시작 전 마지막 확정 시각의 차이로 방향을 고정한다. 매시간 사후에 유리한 방향으로 바꾸지 않는다. 초기 시점의 API 공개 지연은 소급 검증되지 않았다. BTC/ETH 200USD 쌍은 별도 대안이며 기존 가상 재고에 추가 자본처럼 합산하지 않는다.', '',
            '| 마켓 | 고정 방향 | 동일 수량 | 보유 펀딩 USDC | 진입+정리 후 펀딩 USDC | 가격차 변화 평가손익 | 합계 모형 USDC |', '|---|---|---:|---:|---:|---:|---:|']
    for b,h in holds.items():
        lines.append(f"| {b} | {h.get('direction','신호 없음')} | {h.get('quantity','-')} | {number(h.get('gross_usdc'))} | {number(h.get('funding_less_entry_exit_usdc'))} | {number(h.get('basis_mark_pnl_usdc'))} | {number(h.get('total_with_basis_usdc'))} |")
    lines+=['', '고정200USD 명목 정규화 참고값과 168시간 환산은 funding-data.json에 별도 저장한다. 실제 같은 수량을 들고 있는 쌍의 시간별 명목은 가격에 따라 변한다.',
            '진입·정리는 공개 호가 VWAP/수수료 추정이다. 진입 자료의 두 거래소 시각 차이·미관측 전송 지연·Ondo oracle 대신 직전 mid를 쓴 근사가 남아 있다. 이 표를 확정 수익이나 무위험 수익으로 보지 않는다.',
            '누락 시간은 위 시간 수 및 funding-data.json의 missing_hours/rows에서 확인한다. 주간 전체가 끝나기 전의 누적액을 1주 실제 수익으로 표시하지 않는다.']
    if paper_data is not None:
        combined=[]
        lines+=['', '## 기존 6개 조건에 같은 가상 재고의 확정률 펀딩 반영', '',
                '기존 예상 펀딩을 제거하고 확정률 기반 추정으로 교체한다. 200USD 별도 쌍의 이익을 덧붙이지 않는다. 모형별 12행을 보존하며 실제 수익이 아니다.', '',
                '| 마켓 | bps | 모형 | 펀딩 전 순손익 범위 | 확정률 기반 펀딩 | 펀딩 후 순손익 범위 |', '|---|---:|---|---|---:|---|']
        for row in paper_data['rows']:
            f=scenario_funding(comparison,data['rows'],row['base'],row['distance_bps'],row['model'],paper_data['updated_ts'],prices=prices)
            credit=D(f['credit_usdc']) if f['credit_usdc'] is not None else None
            low=high=None
            if (not paper_data.get('interruption_gaps') and row.get('reward_accrued_low_usdc') is not None and row.get('reward_accrued_high_usdc') is not None
                    and row.get('mark_pnl_ex_reward_funding_usd') is not None and row.get('exit_cost_estimate_usd') is not None):
                mark=D(row['mark_pnl_ex_reward_funding_usd']);exit_cost=D(row['exit_cost_estimate_usd'])
                low=mark+D(row['reward_accrued_low_usdc'])-exit_cost
                high=mark+D(row['reward_accrued_high_usdc'])-exit_cost
            netlow=low+credit if low is not None and credit is not None else None
            nethigh=high+credit if high is not None and credit is not None else None
            combined.append({'base':row['base'],'distance_bps':row['distance_bps'],'model':row['model'],
                'funding_before_low':str(low) if low is not None else None,'funding_before_high':str(high) if high is not None else None,
                'settled_rate_funding':f,'net_with_funding_low':str(netlow) if netlow is not None else None,
                'net_with_funding_high':str(nethigh) if nethigh is not None else None})
            lines.append(f"| {row['base']} | {row['distance_bps']} | {row['model']} | {number(low)}~{number(high)} | {number(credit)} | {number(netlow)}~{number(nethigh)} |")
        result['paper_combined']=combined
    (folder/'funding-data.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'FUNDING_REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return result
