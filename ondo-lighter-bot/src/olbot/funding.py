"""공개 확정 펀딩 이력. Decimal 시간당 비율, 누락과 실제 0을 구분한다."""
from __future__ import annotations
from decimal import Decimal as D
from datetime import datetime, timezone
import statistics


def finite(value):
    result=D(str(value))
    if not result.is_finite(): raise ValueError('nonfinite funding data')
    return result


def hour_stamp(timestamp):
    if isinstance(timestamp,str):
        parsed=datetime.fromisoformat(timestamp.replace('Z','+00:00'))
        if parsed.tzinfo is None: raise ValueError('funding timezone required')
        stamp=parsed.timestamp()
    else:
        stamp=int(timestamp)
    hour=int(stamp//3600)*3600
    if not 0<=stamp-hour<60 or stamp<1_000_000_000 or stamp>10_000_000_000:
        raise ValueError('not a seconds-based hourly settlement')
    return hour


def parse_ondo_history(data,market):
    if data.get('success') is not True or not isinstance(data['result'],list): raise ValueError('Ondo funding response failed')
    if (data.get('pageInfo') or {}).get('nextCursor'): raise ValueError('Ondo funding page incomplete')
    rows={}
    for row in data['result']:
        if row['market']!=market: raise ValueError('Ondo funding market mismatch')
        h=hour_stamp(row['time']);rate=finite(row['fundingRate'])
        if h in rows: raise ValueError('duplicate Ondo funding hour')
        rows[h]={'hour':h,'rate':str(rate),'settled_ts':datetime.fromisoformat(row['time'].replace('Z','+00:00')).timestamp()}
    return rows


def parse_lighter_history(data):
    if data.get('code')!=200 or data.get('resolution')!='1h' or not isinstance(data['fundings'],list):
        raise ValueError('Lighter funding response failed')
    rows={}
    for row in data['fundings']:
        h=hour_stamp(row['timestamp']);percent=finite(row['rate']);value=finite(row['value'])
        if percent<0 or value<0 or row['direction'] not in ('long','short'): raise ValueError('Lighter funding sign/amount unknown')
        sign=D(1) if row['direction']=='long' else D(-1)
        rate=sign*percent/100
        if h in rows or (rate==0 and value!=0): raise ValueError('Lighter funding inconsistent')
        rows[h]={'hour':h,'rate':str(rate),'per_base_value':str(sign*value),'direction':row['direction'],'settled_ts':int(row['timestamp'])}
    return rows


def make_hours(base,ondo,lighter,start,end,now):
    first=int(start//3600)*3600+3600
    last=int(min(end,now)//3600)*3600
    rows=[]
    for h in range(first,last+1,3600):
        o,l=ondo.get(h),lighter.get(h)
        rows.append({'base':base,'hour':h,'hour_utc':datetime.fromtimestamp(h,timezone.utc).isoformat(),
                     'ondo_rate':o['rate'] if o else None,'lighter_rate':l['rate'] if l else None,
                     'lighter_per_base_value':l.get('per_base_value') if l else None,
                     'ondo_settled_ts':o.get('settled_ts',h) if o else None,
                     'lighter_settled_ts':l.get('settled_ts',h) if l else None,
                     'difference':str(D(o['rate'])-D(l['rate'])) if o and l else None,
                     'missing':[venue for venue,row in (('ondo',o),('lighter',l)) if row is None]})
    return rows


def history_window(rows,start,end):
    # 실제 Lighter count_back 응답은 start_timestamp보다 앞선 자료도 돌려준다.
    # 원문은 보존하되 목표 창 밖 과거/미래를 통계·방향 신호에 넣지 않는다.
    return {h:r for h,r in rows.items() if start<=h<=end}


def summarize(rows,seed_difference=None):
    good=[r for r in rows if r['difference'] is not None]
    diffs=[D(r['difference']) for r in good]
    counts={-1:0,0:0,1:0};longest={-1:0,1:0}
    last_h=None;last_sign=None;streak=0
    for r in rows:
        if r['difference'] is None:
            last_h=last_sign=None;streak=0;continue
        d=D(r['difference']);sign=1 if d>0 else (-1 if d<0 else 0)
        counts[sign]+=1
        streak=streak+1 if sign and last_sign==sign and last_h==r['hour']-3600 else (1 if sign else 0)
        if sign: longest[sign]=max(longest[sign],streak)
        last_h,last_sign=r['hour'],sign
    total=sum(diffs,D(0)) if diffs else None
    seed=finite(seed_difference) if seed_difference is not None else None
    direction=1 if seed and seed>0 else (-1 if seed and seed<0 else None)
    gross=D(200)*total*direction if total is not None and direction else None
    missing=len(rows)-len(good)
    return {'hours_expected':len(rows),'hours_paired':len(good),'hours_missing':missing,
            'mean_hourly_difference':str(sum(diffs)/len(diffs)) if diffs else None,
            'median_hourly_difference':str(statistics.median(diffs)) if diffs else None,
            'positive_hours':counts[1],'negative_hours':counts[-1],'zero_hours':counts[0],
            'dominant_sign':('positive' if counts[1]>counts[-1] else 'negative' if counts[-1]>counts[1] else 'zero' if diffs and counts[0]==len(diffs) else 'tie') if diffs else None,
            'dominant_sign_fraction':str(D(max(counts[1],counts[-1]))/len(good)) if good else None,
            'longest_positive_hourly_streak':longest[1],'longest_negative_hourly_streak':longest[-1],
            'seed_difference':str(seed) if seed is not None else None,
            'fixed_direction':'ondo_short_lighter_long' if direction==1 else ('ondo_long_lighter_short' if direction==-1 else None),
            'fixed_200_known_subtotal_usdc':str(gross) if gross is not None else None,
            'fixed_200_full_window_usdc':str(gross) if gross is not None and not missing else None,
            'fixed_ondo_short_known_subtotal_usdc':str(D(200)*total) if total is not None else None,
            'fixed_ondo_long_known_subtotal_usdc':str(-D(200)*total) if total is not None else None,
            'fixed_200_168h_extrapolation_usdc':str(D(200)*sum(diffs)/len(diffs)*168*direction) if diffs and direction else None}


def paired_fee_credit(ondo_position,lighter_position,ondo_mid,ondo_rate,lighter_per_base):
    """확정 비율 × 정각 직전 가상 재고. Ondo oracle 대신 mid를 쓴 근사다."""
    if not ondo_position and not lighter_position: return D(0)
    if ondo_position and (ondo_mid is None or ondo_rate is None): return None
    if lighter_position and lighter_per_base is None: return None
    return -(ondo_position*(ondo_mid or 0)*(ondo_rate or 0)+lighter_position*(lighter_per_base or 0))
