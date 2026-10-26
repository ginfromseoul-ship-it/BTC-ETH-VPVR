#!/usr/bin/env python3
"""BTC/ETH V4 prospective observation and PAPER evaluation (never orders).

Uses existing ~15m collector JSON. The output is a prospective event ledger,
not a realtime trade feed or a validated strategy. Only first-seen snapshots
are appended; entries use the NEXT captured snapshot, never signal close.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
SYMBOLS = ('BTC', 'ETH')
HORIZONS = (1, 4, 24)
COSTS_PCT = (0.14, 0.22, 0.35)
VERSION = 'v4_prospective_paper_2026-10-26_01'
TIMING_GUARD_REVISION = '2026-10-26-v4.1-first-seen-proxy'
MAX_SOURCE_AGE_MIN = 35
MAX_BAR_SKEW_MIN = 15
ENTRY_DEADLINE_MIN = 45
OUTCOME_DEADLINE_MIN = 45


def parse_time(value):
    if not isinstance(value, str): raise ValueError('timestamp must be UTC string')
    x = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if x.tzinfo is None: raise ValueError('naive timestamp forbidden')
    return x.astimezone(UTC)


def iso(x):
    return x.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def num(x):
    try:
        if isinstance(x, bool) or x is None: return None
        v=float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):return None


def read_json(path):
    with Path(path).open(encoding='utf8') as f:return json.load(f)


def percentage_buy(bars):
    buys = sum(num(b.get('taker_buy_quote_usdt')) or 0 for b in bars)
    sells = sum(num(b.get('taker_sell_quote_usdt')) or 0 for b in bars)
    if any(num(b.get('taker_buy_quote_usdt')) is None or num(b.get('taker_sell_quote_usdt')) is None
           or num(b.get('taker_buy_quote_usdt')) < 0 or num(b.get('taker_sell_quote_usdt')) < 0 for b in bars):
        return None
    return round(100*buys/(buys+sells),4) if buys+sells>0 else None


def invalidation_from_closes(bars):
    """Low of 48 available CLOSES, not actual traded low or executable stop."""
    x=[num(b.get('close')) for b in bars]
    if len(x)!=48 or any(v is None or v<=0 for v in x):return None
    return round(min(x)*0.998, 6)


def feature_snapshot(spot, deriv, symbol, asof):
    asset=spot.get('assets',{}).get(symbol+'USDT',{})
    future=deriv.get('assets',{}).get(symbol,{})
    sf=asset.get('spot',{}).get('flow',{})
    ff=future.get('futures_flow',{})
    oi=future.get('open_interest',{})
    liq=future.get('liquidations',{})
    depth=asset.get('spot',{}).get('depth',{})
    bars=sf.get('last_5m_bars',[])
    obs={'version':VERSION,'symbol':symbol,'first_seen_utc':iso(asof),
         'spot_bar_end_utc':sf.get('last_closed_bar_utc'),
         'futures_bar_end_utc':ff.get('last_complete_utc'),
         'reference_close_usdt':num(sf.get('latest_close_usdt')),
         'state':'DATA_UNAVAILABLE','research_only':True,
         'not_an_order_or_buy_recommendation':True,
         'evidence':{},'missing':[]}
    err=[]
    try:st=parse_time(obs['spot_bar_end_utc'])
    except (TypeError,ValueError):st=None;err.append('missing_spot_timestamp')
    try:ft=parse_time(obs['futures_bar_end_utc'])
    except (TypeError,ValueError):ft=None;err.append('missing_futures_timestamp')
    if st and (asof-st).total_seconds() / 60 > MAX_SOURCE_AGE_MIN or st and st>asof+timedelta(minutes=1):
        err.append('stale_or_future_spot')
    if ft and ((asof-ft).total_seconds()/60 > MAX_SOURCE_AGE_MIN or ft>asof+timedelta(minutes=1)):
        err.append('stale_or_future_futures')
    if st and ft and abs((st-ft).total_seconds()) > MAX_BAR_SKEW_MIN*60:
        err.append('unsynchronized_spot_futures')
    # Source generation and component data must be independently time-valid.
    for src_name,stamp in [('spot_collected',spot.get('collected_utc')),
                           ('derivatives_generated',deriv.get('generated_utc')),
                           ('spot_1h_end',sf.get('windows',{}).get('1h',{}).get('end_utc')),
                           ('spot_4h_end',sf.get('windows',{}).get('4h',{}).get('end_utc')),
                           ('futures_1h_end',ff.get('windows',{}).get('1h',{}).get('end_utc'))]:
        try:
            when=parse_time(stamp)
            if when>asof+timedelta(minutes=1) or (asof-when)>timedelta(minutes=MAX_SOURCE_AGE_MIN):
                err.append('stale_or_future_'+src_name)
        except (TypeError,ValueError):err.append('invalid_'+src_name)
    # Liquidation and OI source periods have their own timestamps; do not
    # accept old aggregates masked by a recently generated JSON file.
    for src_name,stamp,limit in [('oi_history',oi.get('history_asof_utc'),60),
                                ('liquidations_1h',liq.get('windows',{}).get('1h',{}).get('end_utc'),90)]:
        try:
            when=parse_time(stamp)
            if when>asof+timedelta(minutes=1) or asof-when>timedelta(minutes=limit):
                err.append('stale_or_future_'+src_name)
        except (TypeError,ValueError):err.append('invalid_'+src_name)
    if st:
        for name,expected in [('spot_1h_end',sf.get('windows',{}).get('1h',{}).get('end_utc')),
                              ('spot_4h_end',sf.get('windows',{}).get('4h',{}).get('end_utc'))]:
            if expected != obs['spot_bar_end_utc']:err.append('spot_windows_misaligned')
    if ft and ff.get('windows',{}).get('1h',{}).get('end_utc')!=obs['futures_bar_end_utc']:
        err.append('futures_windows_misaligned')
    if sf.get('windows',{}).get('1h',{}).get('status')!='ok' or sf.get('windows',{}).get('4h',{}).get('status')!='ok':
        err.append('spot_windows_not_verified')
    if ff.get('windows',{}).get('1h',{}).get('status')!='ok':err.append('futures_flow_not_verified')
    if oi.get('changes',{}).get('1h',{}).get('status')!='ok':err.append('oi_not_verified')
    if liq.get('windows',{}).get('1h',{}).get('status')!='ok':err.append('liquidations_not_verified')
    if len(bars)<48:err.append('missing_last48_spot_bars')
    if len(bars)>=48:
        bars=bars[-48:]
        try:
            times=[parse_time(b['start_utc']) for b in bars]
            if any((b-a).total_seconds()!=300 for a,b in zip(times,times[1:])) or (times[-1]+timedelta(minutes=5)!=st):
                err.append('noncontiguous_spot_bars')
        except (TypeError,ValueError,KeyError):err.append('invalid_spot_bar_timestamps')
    if num(obs['reference_close_usdt']) is None or obs['reference_close_usdt']<=0:err.append('bad_spot_price')
    w1=sf.get('windows',{}).get('1h',{})
    w4=sf.get('windows',{}).get('4h',{})
    f1=ff.get('windows',{}).get('1h',{})
    o1=oi.get('changes',{}).get('1h',{})
    l1=liq.get('windows',{}).get('1h',{})
    d01=depth.get('depth_bands',{}).get('plus_minus_0.1pct',{})
    recent_share=percentage_buy(bars[-3:]) if len(bars)>=3 else None
    spot_share=num(w1.get('buy_share_quote_pct'))
    fut_share=num(f1.get('taker_buy_share_pct'))
    change4=num(w4.get('price_change_pct'))
    oi_change=num(o1.get('native_units_pct'))
    liq_long=num(l1.get('long_liquidations_usd'))
    liq_short=num(l1.get('short_liquidations_usd'))
    floor=invalidation_from_closes(bars)
    price=obs['reference_close_usdt']
    distance_from_low=round(100*(price/min(num(b['close']) for b in bars)-1),4) if len(bars)==48 and all(num(b['close']) is not None and num(b['close'])>0 for b in bars) and price else None
    # Snapshot order book is context only, not proof of passive execution or absorption.
    depth_ratio=num(d01.get('bid_ask_ratio')) if d01.get('coverage_complete_bid') and d01.get('coverage_complete_ask') else None
    obs['evidence']={
      'spot_buy_share_1h_pct':spot_share,'spot_buy_share_last15m_pct':recent_share,
      'futures_buy_share_1h_pct':fut_share,'spot_price_change_4h_pct':change4,
      'oi_native_change_1h_pct':oi_change,
      'reported_long_liquidations_1h_usd':liq_long,
      'reported_short_liquidations_1h_usd':liq_short,
      'depth_bid_ask_ratio_0_1pct_snapshot':depth_ratio,
      'price_distance_from_recent_close_low_pct':distance_from_low,
      'close_based_invalidation_reference_usdt':floor,
      'risk_to_reference_pct':round(100*(price-floor)/price,4) if floor and price else None}
    if any(v is None for v in (recent_share,spot_share,fut_share,change4,oi_change,liq_long,liq_short,floor,distance_from_low)):
        err.append('missing_core_features')
    if any(v is not None and (v < 0 or v>100) for v in (recent_share,spot_share,fut_share)):
        err.append('invalid_taker_buy_share')
    if liq_long is not None and liq_long<0 or liq_short is not None and liq_short<0:err.append('negative_liquidations')
    spot_issues={'missing_spot_timestamp','stale_or_future_spot','invalid_spot_collected',
                 'stale_or_future_spot_collected','invalid_spot_1h_end',
                 'invalid_spot_4h_end','stale_or_future_spot_1h_end',
                 'stale_or_future_spot_4h_end','spot_windows_misaligned',
                 'spot_windows_not_verified','missing_last48_spot_bars',
                 'noncontiguous_spot_bars','invalid_spot_bar_timestamps',
                 'bad_spot_price'}
    # Paper marks use independently validated spot candles; an unavailable
    # futures/OI feed may block signals but need not invalidate a spot mark.
    obs['spot_price_observation_usable']=(not any(e in spot_issues for e in err) and
                                          obs['reference_close_usdt'] is not None and
                                          obs['reference_close_usdt']>0)
    if err:
        obs['missing']=sorted(set(err));return obs
    # These exploratory thresholds are registration-only. No historical edge is claimed.
    prior_sell=change4<=-1.0
    confirmed=(prior_sell and recent_share>=54 and spot_share>=50 and fut_share>=50
               and oi_change>=-0.2 and distance_from_low>=0.35
               and obs['evidence']['risk_to_reference_pct']<=3.0)
    # Depth is OPTIONAL contextual evidence; no orderbook absorption inferred.
    emerging=(prior_sell and recent_share>=51 and distance_from_low>=0.15)
    if confirmed:state='PAPER_CANDIDATE_UNVALIDATED'
    elif emerging:state='REVERSAL_WATCH'
    elif prior_sell and spot_share<50 and fut_share<50:state='SELLING_CONTINUES'
    elif prior_sell:state='MIXED_WAIT'
    else:state='NO_DOWNTURN_SETUP'
    obs['state']=state
    obs['missing']=[]
    obs['evidence']['price_trigger_hypothesis']='4h dip <= -1pct for either symbol; shared research gate'
    return obs


def parse_jsonl(p):
    if not p.exists():return []
    return [json.loads(x) for x in p.read_text(encoding='utf8').splitlines() if x.strip()]


def append_new(p,records,id_key):
    if not records:return 0
    p.parent.mkdir(parents=True,exist_ok=True)
    existing=parse_jsonl(p)
    keys={r[id_key] for r in existing}
    add=[r for r in records if r[id_key] not in keys and not keys.add(r[id_key])]
    if add:
        with p.open('a',encoding='utf8') as f:
            for r in add:f.write(json.dumps(r,ensure_ascii=False,allow_nan=False,sort_keys=True)+'\n')
    return len(add)


def daily(out,folder,day):return out/folder/(day+'.jsonl')

def recent(out,folder,asof,days=4):
    data=[]
    for k in range(days+1):data+=parse_jsonl(daily(out,folder,(asof.date()-timedelta(days=k)).isoformat()))
    return data


def chronological_snapshots(items):
    # First-seen-only, no re-writing historical snapshots; skip unavailable prices.
    return sorted((x for x in items if x.get('spot_price_observation_usable') is True and x.get('spot_bar_end_utc')),
                  key=lambda x:(x['spot_bar_end_utc'],x['symbol']))


def pipeline(spot,deriv,asof,out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    parsed=[]
    evaluated=[]
    for sym in SYMBOLS:
        x=feature_snapshot(spot,deriv,sym,asof)
        evaluated.append(x)
        # No key if source timestamp unparseable: do not create prospectively date-misidentified records.
        if x['spot_bar_end_utc']:
            try:parse_time(x['spot_bar_end_utc'])
            except (TypeError, ValueError):continue
            x['snapshot_id']=hashlib.sha256((VERSION+'|'+sym+'|'+x['spot_bar_end_utc']).encode()).hexdigest()[:24]
            parsed.append(x)
    today=asof.date().isoformat()
    old=recent(out,'snapshots',asof,3)
    seen={x['snapshot_id'] for x in old}
    fresh=[x for x in parsed if x['snapshot_id'] not in seen]
    # Fail closed if sources are too old or already observed; never fabricate a new observation.
    fresh=[x for x in fresh if parse_time(x['spot_bar_end_utc'])<=asof+timedelta(minutes=1)]
    append_new(daily(out,'snapshots',today),fresh,'snapshot_id')
    allsnap=chronological_snapshots(old+fresh)
    existing_entries=recent(out,'entries',asof,3)
    entries_by={x['snapshot_id'] for x in existing_entries}
    # Paper entry at first subsequent independently captured spot close, not at signal close.
    entries=[]
    last_cohort_signal={}
    selected_cohorts=0
    for s in allsnap:
        if s['state']!='PAPER_CANDIDATE_UNVALIDATED':continue
        signal_time=parse_time(s['spot_bar_end_utc'])
        signal_seen=parse_time(s['first_seen_utc'])
        # Anchor cohort to actual first-seen decision time, not the already closed source bar.
        last=last_cohort_signal.get(s['symbol'])
        if last is not None and signal_seen-last<timedelta(hours=24):continue
        last_cohort_signal[s['symbol']]=signal_seen
        selected_cohorts+=1
        if s['snapshot_id'] in entries_by:continue
        next_snap=next((x for x in allsnap if x['symbol']==s['symbol']
                         # The *price observation* itself must be later than when
                         # the research candidate first became known. A bar
                         # between signal close and first_seen is NOT actionable.
                         and parse_time(x['spot_bar_end_utc'])>signal_seen
                         and parse_time(x['spot_bar_end_utc'])<=signal_seen+timedelta(minutes=ENTRY_DEADLINE_MIN)
                         and parse_time(x['first_seen_utc'])>signal_seen
                         and parse_time(x['first_seen_utc'])<=signal_seen+timedelta(minutes=ENTRY_DEADLINE_MIN)
                         and parse_time(x['first_seen_utc'])>=parse_time(x['spot_bar_end_utc'])
                         and x['reference_close_usdt']>0),None)
        if next_snap is None:
            if asof>signal_seen+timedelta(minutes=ENTRY_DEADLINE_MIN):
                entries.append({'snapshot_id':s['snapshot_id'],'symbol':s['symbol'],
                                'signal_utc':s['spot_bar_end_utc'],
                                'signal_first_seen_utc':s['first_seen_utc'],
                                'entry_utc':None,'entry_price_bar_end_utc':None,'entry_first_seen_utc':None,
                                'entry_reference_price_usdt':None,'entry_reference_risk_pct':None,
                                'invalidation_reference_usdt':s.get('evidence',{}).get('close_based_invalidation_reference_usdt'),
                                'target_2r_reference_usdt':None,
                                'paper_entry_status':'UNAVAILABLE_NO_TIMELY_NEXT_OBSERVATION',
                                'entry_method':'no causally later and timely recorded observation; excluded, not fabricated',
                                'model_version':VERSION,'state':s['state']})
            continue
        ep=next_snap['reference_close_usdt']
        floor=s.get('evidence',{}).get('close_based_invalidation_reference_usdt')
        riskpct=round(100*(ep-floor)/ep,4) if floor and ep>0 else None
        eligible=riskpct is not None and 0<riskpct<=3.0
        entries.append({'snapshot_id':s['snapshot_id'],'symbol':s['symbol'],
                        'signal_first_seen_utc':s['first_seen_utc'],
                        # Entry is an observed paper MARK, NOT an exchange fill.
                        # Time anchor is when the price became available to our system.
                        'entry_utc':next_snap['first_seen_utc'],
                        'entry_price_bar_end_utc':next_snap['spot_bar_end_utc'],
                        'entry_first_seen_utc':next_snap['first_seen_utc'],
                        'entry_reference_price_usdt':ep,
                        'entry_reference_risk_pct':riskpct,
                        'invalidation_reference_usdt':floor,
                        'target_2r_reference_usdt':round(ep+2*(ep-floor),6) if eligible else None,
                        'paper_entry_status':'ELIGIBLE_PAPER_REFERENCE' if eligible else 'BLOCKED_BY_ENTRY_RISK',
                        'signal_utc':s['spot_bar_end_utc'],'state':s['state'],
                        'entry_method':'first later price bar whose close AND first-seen time follow signal first_seen; proxy NOT fill',
                        'model_version':VERSION})
    append_new(daily(out,'entries',today),entries,'snapshot_id')
    entries=existing_entries+entries
    old_outcomes=recent(out,'outcomes',asof,3)
    outcome_ids={x['outcome_id'] for x in old_outcomes}
    outcomes=[]
    for entry in entries:
        if entry.get('paper_entry_status')!='ELIGIBLE_PAPER_REFERENCE':continue
        ent_time=parse_time(entry['entry_utc']);entry_px=num(entry['entry_reference_price_usdt'])
        if not entry_px or entry_px<=0:continue
        for h in HORIZONS:
            target=ent_time+timedelta(hours=h)
            oid=entry['snapshot_id']+'_'+str(h)+'h'
            if oid in outcome_ids:continue
            options=[x for x in allsnap if x['symbol']==entry['symbol'] and x['reference_close_usdt']>0
                     and target<=parse_time(x['spot_bar_end_utc'])<=target+timedelta(minutes=OUTCOME_DEADLINE_MIN)
                     and parse_time(x['first_seen_utc'])>=parse_time(x['spot_bar_end_utc'])
                     and parse_time(x['first_seen_utc'])<=target+timedelta(minutes=OUTCOME_DEADLINE_MIN)]
            outcome=None
            if options:
                x=min(options,key=lambda v:v['spot_bar_end_utc'])
                raw=100*(x['reference_close_usdt']/entry_px-1)
                outcome={'status':'OBSERVED_PROXY','price_utc':x['spot_bar_end_utc'],
                         'price_first_seen_utc':x['first_seen_utc'],
                         'price_usdt':x['reference_close_usdt'],
                         'gross_return_pct':round(raw,5),
                         'net_return_pct_by_cost':{str(cost):round(raw-cost,5) for cost in COSTS_PCT}}
            elif asof>target+timedelta(minutes=OUTCOME_DEADLINE_MIN):
                outcome={'status':'UNAVAILABLE_MISSING_TIMELY_OBSERVATION','price_utc':None,
                         'price_first_seen_utc':None,
                         'price_usdt':None,'gross_return_pct':None,'net_return_pct_by_cost':None}
            if outcome:
                outcomes.append({'outcome_id':oid,'snapshot_id':entry['snapshot_id'],'symbol':entry['symbol'],
                                 'entry_utc':entry['entry_utc'],'horizon_hours':h,
                                 'due_utc':iso(target),'recorded_utc':iso(asof),**outcome})
                outcome_ids.add(oid)
    append_new(daily(out,'outcomes',today),outcomes,'outcome_id')
    current={x['symbol']:x for x in evaluated}
    result={'version':VERSION,'timing_guard_revision':TIMING_GUARD_REVISION,
            'generated_utc':iso(asof),'cadence':'scheduled approximately 15min; not tick realtime',
            'research_only':True,'buy_alerts_enabled':False,'orders_enabled':False,
            'executable_buy_prices_available':False,
            'input_sources':{'spot_collected_utc':spot.get('collected_utc'),
                             'futures_collected_utc':deriv.get('generated_utc')},
            'states':current,'new_snapshots_saved':len(fresh),
            'candidate_cohort_cooldown_hours':24,
            'new_paper_entries':len(entries)-len(existing_entries),
            'new_forward_outcomes':len(outcomes),
            'limitations':['Prospective observations begin when workflow first runs; historical 24m is NOT used to claim live performance',
                           '15m scheduled REST snapshots do not establish absorption/order cancellations or actual execution timing',
                           'Liquidation figures are vendor interval aggregates, not comprehensive forceOrder tape',
                           'Paper entry mark requires both source bar close and first-seen to be strictly after signal FIRST SEEN; the price is still a delayed historical close, not an executable quote',
                           'Entry/exit are NEXT RECORDED spot closes and can be delayed or missing; no fills/stop execution modeled',
                           'Paper returns are simple notional differences minus illustrative roundtrip costs, not realized P&L',
                           'Features use asynchronous spot/futures data within up to 15m; only research proxy',
                           'Candidate paper cohort has 24h per-symbol cooldown to reduce overlapping outcomes',
                           'No profitable buy edge established by prior V3; candidate does NOT mean buy']}
    (out/'latest.json').write_text(json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf8')
    return result


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--spot',type=Path,default=Path('data/market_flow_latest.json'))
    p.add_argument('--derivatives',type=Path,default=Path('data/coinalyze_derivatives_latest.json'))
    p.add_argument('--out',type=Path,default=Path('data/v4_monitor'))
    a=p.parse_args()
    report=pipeline(read_json(a.spot),read_json(a.derivatives),datetime.now(UTC),a.out)
    print('V4_PROSPECTIVE '+json.dumps({s:{'state':x['state'],'missing':x['missing'],
        'reference_close_usdt':x['reference_close_usdt']} for s,x in report['states'].items()}))
    print('NEW_SNAPSHOTS',report['new_snapshots_saved'],'PAPER_ENTRIES',report['new_paper_entries'],
          'FORWARD_OUTCOMES',report['new_forward_outcomes'])

if __name__=='__main__':main()
