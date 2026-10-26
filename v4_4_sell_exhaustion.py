#!/usr/bin/env python3
"""V4.4 market-state diagnostics: prospective and PAPER ONLY.

Reads immutable V4.2 observations. Values are aggregated REST snapshots, not
exchange-time order flow. 'Easing'/'recovery' are exploratory proxies, NOT
proof of book absorption, liquidation exhaustion, or executable buy signals.
Decisions are frozen when RUN, not backdated to 5m candle close/first-seen.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from v4_3_cohort_compare import (parse_time, iso, numeric, observed_snapshot,
                                  snapshots_from_v4, append_once, day_path,
                                  recent_rows)

UTC=timezone.utc
VERSION='v4_4_sequential_market_state_2026-10-26_locked_01'
COSTS=(0.14,0.22,0.35)
HOURS=(1,4,24)
MAX_DECISION_AGE=timedelta(minutes=25)
MAX_PAIR_GAP=timedelta(minutes=90)
DIP_CONTEXT=timedelta(hours=2)
ENTRY_WINDOW=timedelta(minutes=45)
OUTCOME_WINDOW=timedelta(minutes=45)
STATE_RECOVERY='SPOT_RECOVERY_PROXY'
STAGES=('NO_RECENT_DIP','BASELINE_PENDING','GAP_UNVERIFIED','SOURCE_UNVERIFIED',
        'SELLING_CONTINUES_PROXY','SELL_PRESSURE_EASING_PROXY',STATE_RECOVERY,
        'FUTURES_LED_BOUNCE_RISK','MIXED_WAIT')


def safe_num(x):return numeric(x)


def evidence(s,key):return safe_num(s.get('evidence',{}).get(key))


def usable_for_state(s):
    """Bad/missing derivatives disable state even if spot mark still usable."""
    if s.get('state')=='DATA_UNAVAILABLE' or s.get('missing'):
        return False
    mandatory=('spot_buy_share_1h_pct','spot_buy_share_last15m_pct',
      'futures_buy_share_1h_pct','spot_price_change_4h_pct','oi_native_change_1h_pct',
      'reported_long_liquidations_1h_usd','reported_short_liquidations_1h_usd')
    if not all(evidence(s,k) is not None for k in mandatory):return False
    if not all(0<=evidence(s,k)<=100 for k in mandatory[:3]):return False
    if any(evidence(s,k)<0 for k in mandatory[-2:]):return False
    try:
        ft=parse_time(s['futures_bar_end_utc']);st=parse_time(s['spot_bar_end_utc'])
        if abs((ft-st).total_seconds())>15*60:return False
    except (ValueError, TypeError, KeyError):return False
    return True


def direction(s):
    """Classify without looking at later snapshots, using pre-registered gates."""
    return evidence(s,'spot_price_change_4h_pct') is not None and evidence(s,'spot_price_change_4h_pct')<=-1


def decide(snapshot, preceding, now):
    """One observed snapshot, previous *already seen* observations, no future data.

    previous can include pre-bootstrap observations as context, NEVER as an
    earlier V4.4 decision. 'now' is actual execution/detection time.
    """
    symbol=snapshot['symbol'];seen=parse_time(snapshot['first_seen_utc'])
    hist=sorted((x for x in preceding if x['symbol']==symbol and
        parse_time(x['first_seen_utc'])<seen),key=lambda x:x['first_seen_utc'])
    prior=next((x for x in reversed(hist) if usable_for_state(x)),None)
    out={'version':VERSION,'decision_id':hashlib.sha256((VERSION+'|'+snapshot['snapshot_id']).encode()).hexdigest()[:24],
        'snapshot_id':snapshot['snapshot_id'],'symbol':symbol,'first_seen_utc':snapshot['first_seen_utc'],
        'decision_recorded_utc':iso(now),'reference_close_usdt':safe_num(snapshot['reference_close_usdt']),
        'spot_bar_end_utc':snapshot['spot_bar_end_utc'],'stage':'SOURCE_UNVERIFIED',
        'no_orders':True,'research_only':True,'entry_signal':False,'evidence':{},
        'limitations':'periodic sampled taker volume / reported liquidation / book snapshot, NOT order-level absorption'}
    if not usable_for_state(snapshot) or not observed_snapshot(snapshot,now):return out
    if now-seen>MAX_DECISION_AGE or now<seen:
        out['stage']='SOURCE_UNVERIFIED';out['reason']='decision_not_timely';return out
    if prior is None:
        out['stage']='BASELINE_PENDING';return out
    age=seen-parse_time(prior['first_seen_utc'])
    if (age>MAX_PAIR_GAP or age<=timedelta(0) or
        parse_time(prior['spot_bar_end_utc'])>=parse_time(snapshot['spot_bar_end_utc'])):
        out['stage']='GAP_UNVERIFIED';out['evidence']['baseline_gap_min']=round(age.total_seconds()/60,2);return out
    dip=any(direction(x) for x in [snapshot]+hist if seen-DIP_CONTEXT<=parse_time(x['first_seen_utc'])<=seen and usable_for_state(x))
    sp_now=evidence(snapshot,'spot_buy_share_1h_pct')
    recent=evidence(snapshot,'spot_buy_share_last15m_pct')
    futures=evidence(snapshot,'futures_buy_share_1h_pct')
    oi=evidence(snapshot,'oi_native_change_1h_pct')
    prev_sp=evidence(prior,'spot_buy_share_1h_pct')
    prev_recent=evidence(prior,'spot_buy_share_last15m_pct')
    prev_price=safe_num(prior.get('reference_close_usdt'))
    price=safe_num(snapshot.get('reference_close_usdt'))
    if not prev_price or not price or prev_price<=0 or price<=0:
        out['stage']='SOURCE_UNVERIFIED';return out
    price_change=100*(price/prev_price-1)
    prior_weak=prev_sp<=48 or prev_recent<=47
    # A fresh long-liquidation spike alone cannot prove 'forced selling ended'.
    out['evidence']={
      'previous_snapshot_id':prior['snapshot_id'],
      'baseline_gap_min':round(age.total_seconds()/60,2),
      'price_change_since_previous_seen_pct':round(price_change,5),
      'spot_taker_buy_share_1h_pct':sp_now,
      'spot_taker_buy_share_last15m_pct':recent,
      'recent_spot_taker_buy_share_change_pp':round(recent-prev_recent,4),
      'futures_taker_buy_share_1h_pct':futures,
      'oi_1h_native_pct_change':oi,
      'reported_long_liquidations_1h_usd':evidence(snapshot,'reported_long_liquidations_1h_usd'),
      'reported_short_liquidations_1h_usd':evidence(snapshot,'reported_short_liquidations_1h_usd'),
      'depth_ratio_context_only':evidence(snapshot,'depth_bid_ask_ratio_0_1pct_snapshot'),
      'recent_4h_dip_context':dip,
      'prior_spot_sell_imbalance':prior_weak}
    if not dip:stage='NO_RECENT_DIP'
    elif price_change>=0.15 and futures>=55 and sp_now<49:
        stage='FUTURES_LED_BOUNCE_RISK'
    elif (prior_weak and price_change>=0.25 and recent>=55 and
          sp_now>=50 and oi>=-1.5):
        stage=STATE_RECOVERY
    elif (prev_recent<50 and recent>=48 and recent-prev_recent>=7
          and price_change>=-0.15):
        stage='SELL_PRESSURE_EASING_PROXY'
    elif recent<=47 and price_change<=-0.20:
        stage='SELLING_CONTINUES_PROXY'
    else:stage='MIXED_WAIT'
    out['stage']=stage
    out['entry_signal']=stage==STATE_RECOVERY
    return out


def closest_after(snapshots, symbol, start, max_wait):
    """Next independently SEEN spot mark with bar closed AFTER decision."""
    marks=[s for s in snapshots if s['symbol']==symbol and
        start<parse_time(s['first_seen_utc'])<=start+max_wait and
        parse_time(s['spot_bar_end_utc'])>start]
    return min(marks,key=lambda s:(s['first_seen_utc'],s['snapshot_id'])) if marks else None


def paper_summary(decisions,entries,outcomes):
    return {symbol:{'decisions':len([d for d in decisions if d['symbol']==symbol]),
        'stages':{stage:sum(d['symbol']==symbol and d['stage']==stage for d in decisions) for stage in STAGES},
        'paper_candidates':sum(d['symbol']==symbol and d['entry_signal'] for d in decisions),
        'last_decision':next(({'stage':d['stage'],'decided_at_utc':d['decision_recorded_utc'],
                                'reference_close_usdt':d['reference_close_usdt'],
                                'entry_signal':d['entry_signal']}
           for d in sorted(decisions,key=lambda x:x['decision_recorded_utc'],reverse=True)
           if d['symbol']==symbol),None),
        'paper_entries':sum(e['symbol']==symbol and e['status']=='REFERENCE_ENTRY_OBSERVED' for e in entries),
        '24h_marks':sum(o['symbol']==symbol and o['hours']==24 and o['status']=='REFERENCE_OUTCOME_OBSERVED' for o in outcomes),
        'mean_24h_net_0_14pct':round(mean([o['net_pct_by_cost']['0.14'] for o in outcomes if o['symbol']==symbol and o['hours']==24 and o['status']=='REFERENCE_OUTCOME_OBSERVED']),5) if any(o['symbol']==symbol and o['hours']==24 and o['status']=='REFERENCE_OUTCOME_OBSERVED' for o in outcomes) else None}
      for symbol in ('BTC','ETH')}


def evaluate(now,root):
    now=now.astimezone(UTC);root=Path(root);ledger=root/'market_state_v4_4';ledger.mkdir(parents=True,exist_ok=True)
    config=ledger/'protocol.json'
    if not config.exists():
        # First run only registers inception: never label pre-existing snapshots ex post.
        config.write_text(json.dumps({'version':VERSION,'inception_utc':iso(now),'no_historical_backfill':True},indent=2)+'\n',encoding='utf8')
        r={'version':VERSION,'status':'BOOTSTRAPPED_NO_HISTORICAL_BACKFILL','generated_utc':iso(now),
           'research_only':True,'orders_enabled':False,'buy_alerts_enabled':False,
           'new_decisions':0,'new_entries':0,'new_outcomes':0,'by_symbol':paper_summary([],[],[])}
        (ledger/'latest.json').write_text(json.dumps(r,indent=2,allow_nan=False)+'\n',encoding='utf8')
        return r
    cfg=json.loads(config.read_text('utf8'))
    if cfg.get('version')!=VERSION or cfg.get('no_historical_backfill') is not True:
        raise ValueError('Do not reset or silently reinterpret V4.4 inception')
    inception=parse_time(cfg['inception_utc'])
    snaps=snapshots_from_v4(root,now)
    decisions=recent_rows(ledger,'decisions',now,days=5)
    keys={d['decision_id'] for d in decisions}
    new_decisions=0
    # Attach contemporaneously registered V4.3 cohort IDs only as comparison metadata.
    # Never use V4.3 outcomes to select or tune a V4.4 signal.
    comparison_cohorts=recent_rows(root/'comparison_v4_3','cohorts',now,days=5)
    for index,s in enumerate(snaps):
        seen=parse_time(s['first_seen_utc'])
        if seen<=inception or now-seen>MAX_DECISION_AGE:continue
        d=decide(s,snaps[:index],now)
        matched=[c for c in comparison_cohorts if c.get('symbol')==d['symbol'] and
                 parse_time(c['start_seen_utc'])<=now and
                 timedelta(0)<=now-parse_time(c['start_seen_utc'])<=timedelta(hours=4)]
        d['linked_v4_3_cohort_id']=(max(matched,key=lambda c:c['start_seen_utc'])['cohort_id']
                                    if matched else None)
        if d['decision_id'] in keys:continue
        if append_once(day_path(ledger,'decisions',iso(now)),d,'decision_id'):
            new_decisions+=1;keys.add(d['decision_id']);decisions.append(d)
    decisions.sort(key=lambda x:(x['decision_recorded_utc'],x['symbol'],x['decision_id']))
    entries=recent_rows(ledger,'entries',now,days=5);entry_ids={e['entry_id'] for e in entries}
    new_entries=0
    for d in decisions:
        if not d['entry_signal'] or d['decision_id'] in entry_ids:continue
        start=parse_time(d['decision_recorded_utc'])
        mark=closest_after(snaps,d['symbol'],start,ENTRY_WINDOW)
        if not mark and now<=start+ENTRY_WINDOW:continue
        status='REFERENCE_ENTRY_OBSERVED' if mark else 'NO_TIMELY_POST_DECISION_PRICE'
        entry={'entry_id':d['decision_id'],'symbol':d['symbol'],'signal_decision_utc':d['decision_recorded_utc'],
               'decision_id':d['decision_id'],'signal_reference_usdt':d['reference_close_usdt'],
               'entry_seen_utc':mark['first_seen_utc'] if mark else None,
               'entry_price_bar_end_utc':mark['spot_bar_end_utc'] if mark else None,
               'entry_reference_usdt':mark['reference_close_usdt'] if mark else None,
               'status':status,'created_utc':iso(now),'research_only':True}
        if append_once(day_path(ledger,'entries',iso(now)),entry,'entry_id'):
            entry_ids.add(entry['entry_id']);entries.append(entry);new_entries+=1
    outcomes=recent_rows(ledger,'outcomes',now,days=5);ids={o['outcome_id'] for o in outcomes}
    new_outcomes=0
    for entry in entries:
        if entry['status']!='REFERENCE_ENTRY_OBSERVED':continue
        t=parse_time(entry['entry_seen_utc']);p=safe_num(entry['entry_reference_usdt'])
        if not p or p<=0:continue
        for h in HOURS:
            oid=entry['entry_id']+'|'+str(h)+'h'
            if oid in ids:continue
            due=t+timedelta(hours=h)
            mark=closest_after(snaps,entry['symbol'],due-timedelta(microseconds=1),OUTCOME_WINDOW+timedelta(microseconds=1)) if now>=due else None
            # Must not mark before 1h/4h/24h wall-clock AFTER entry first-seen.
            if mark is not None and parse_time(mark['first_seen_utc'])<due:mark=None
            if mark is None and now<=due+OUTCOME_WINDOW:continue
            gross=100*(safe_num(mark['reference_close_usdt'])/p-1) if mark else None
            result={'outcome_id':oid,'entry_id':entry['entry_id'],'symbol':entry['symbol'],
                    'hours':h,'due_utc':iso(due),'recorded_utc':iso(now),
                    'status':'REFERENCE_OUTCOME_OBSERVED' if mark else 'NO_TIMELY_FORWARD_PRICE',
                    'outcome_seen_utc':mark['first_seen_utc'] if mark else None,
                    'outcome_bar_end_utc':mark['spot_bar_end_utc'] if mark else None,
                    'exit_reference_usdt':mark['reference_close_usdt'] if mark else None,
                    'gross_pct':round(gross,5) if gross is not None else None,
                    'net_pct_by_cost':{str(c):round(gross-c,5) for c in COSTS} if gross is not None else None,
                    'paper_only':True}
            if append_once(day_path(ledger,'outcomes',iso(now)),result,'outcome_id'):
                outcomes.append(result);ids.add(oid);new_outcomes+=1
    report={'version':VERSION,'status':'PROSPECTIVE_RESEARCH_ACTIVE','generated_utc':iso(now),
        'inception_utc':cfg['inception_utc'],'research_only':True,'orders_enabled':False,'buy_alerts_enabled':False,
        'new_decisions':new_decisions,'new_entries':new_entries,'new_outcomes':new_outcomes,
        'by_symbol':paper_summary(decisions,entries,outcomes),
        'limitations':['No order-by-order absorption, no verified liquidation exhaustion, no true traded low.',
          'V4.4 exploratory thresholds not fitted or validated as predictive buy advantage.',
          'References are subsequent recorded spot 5-minute CLOSES, never executable fills.',
          'No actual orderbook persistence/cancellations, position sizing, futures funding or liquidation modeling.',
          'Entry and outcome times use first-observed source snapshots; missing price remains missing.',
          'Decisions made at pipeline execution, not earlier candle timestamp.',
          'V4.3 cohort accounting remains independent; compare after enough forward pairs mature.']}
    (ledger/'latest.json').write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf8')
    return report


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,default=Path('data/v4_monitor'))
    a=p.parse_args()
    report=evaluate(datetime.now(UTC),a.root)
    print('V4_4_MARKET_STATE '+json.dumps({k:report[k] for k in ('status','new_decisions','new_entries','new_outcomes','by_symbol')}))

if __name__=='__main__':main()
