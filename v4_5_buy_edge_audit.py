#!/usr/bin/env python3
"""V4.5: locked, cohort-matched BTC/ETH prospective entry-quality audit.

READ ONLY upstream: V4.2 immutable price snapshots, V4.3 cohort/arm ledgers,
V4.4 decisions/entries. WRITE ONLY comparison_v4_5/. No trading API, orders,
or actionable alerts. Common-clock observational proxies, NOT traded fills.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

UTC = timezone.utc
VERSION = 'v4_5_common_clock_buy_edge_2026-10-26_locked_01'
ARMS = ('DIP_NEXT', 'WATCH_NEXT', 'CONFIRM_NEXT', 'V44_SPOT_RECOVERY')
HORIZONS = (4, 24)
COSTS = (0.14, 0.22, 0.35)
ENTRY_SIGNAL_WINDOW = timedelta(hours=4)
PRICE_WINDOW = timedelta(minutes=45)
COVERAGE_GAP = timedelta(minutes=90)
FORCE_FINALIZE = timedelta(hours=36)
RETENTION_DAYS = 9


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError('timestamp required')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('timezone required')
    return dt.astimezone(UTC)


def iso(dt):
    return dt.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def number(value):
    if isinstance(value, bool):
        return None
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def load_rows(root, folder, now, days=RETENTION_DAYS):
    p = Path(root) / folder
    if not p.is_dir():
        return []
    rows = []
    for k in range(days + 1):
        f = p / ((now.date() - timedelta(days=k)).isoformat() + '.jsonl')
        if f.is_file():
            for line in f.read_text(encoding='utf8').splitlines():
                if line.strip():
                    rows.append(json.loads(line))
    return rows


def append_once(root, folder, date, record, id_key):
    p = Path(root) / folder / (date + '.jsonl')
    p.parent.mkdir(parents=True, exist_ok=True)
    old = [json.loads(x) for x in p.read_text(encoding='utf8').splitlines() if x.strip()] if p.exists() else []
    if any(x.get(id_key) == record[id_key] for x in old):
        return False
    with p.open('a', encoding='utf8') as f:
        f.write(json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True) + '\n')
    return True


def spot_usable(row, now):
    if row.get('spot_price_observation_usable') is not True or row.get('symbol') not in ('BTC', 'ETH'):
        return False
    px = number(row.get('reference_close_usdt'))
    if px is None or px <= 0:
        return False
    try:
        seen = parse_time(row['first_seen_utc'])
        bar = parse_time(row['spot_bar_end_utc'])
    except (KeyError, TypeError, ValueError):
        return False
    return bar <= seen <= now and timedelta(0) <= seen - bar <= timedelta(minutes=35)


def common_mark(snaps, symbol, target, now):
    """First independently observed price whose BAR END and FIRST-SEEN follow target."""
    return next((s for s in snaps if s['symbol'] == symbol and
            target <= parse_time(s['spot_bar_end_utc']) and
            target <= parse_time(s['first_seen_utc']) <= min(now, target + PRICE_WINDOW)), None)


def coverage_status(decisions, symbol, start):
    """Conservative 'no recovery signal' requires reasonably continuous V4.4 decisions."""
    eligible = sorted((d for d in decisions if d.get('symbol') == symbol and
        start <= parse_time(d['decision_recorded_utc']) <= start + ENTRY_SIGNAL_WINDOW),
        key=lambda x: x['decision_recorded_utc'])
    times = [parse_time(d['decision_recorded_utc']) for d in eligible]
    invalid_stages = ('SOURCE_UNVERIFIED', 'GAP_UNVERIFIED', 'BASELINE_PENDING')
    if (len(times) < 2 or times[0] - start > COVERAGE_GAP or
        start + ENTRY_SIGNAL_WINDOW - times[-1] > COVERAGE_GAP or
        any(b-a > COVERAGE_GAP for a, b in zip(times, times[1:])) or
        any(d.get('stage') in invalid_stages for d in eligible)):
        return 'INSUFFICIENT_V44_COVERAGE'
    return 'OBSERVED_NO_SPOT_RECOVERY'


def resolved_arm(cohort, arm, v43_entries, v44_decisions, v44_entries):
    """Only explicit prior records may authorize a paper entry or a no-signal."""
    cid = cohort['cohort_id']; start = parse_time(cohort['start_seen_utc']); symbol = cohort['symbol']
    if arm != 'V44_SPOT_RECOVERY':
        entry = next((x for x in v43_entries if x.get('cohort_id') == cid and x.get('arm') == arm), None)
        if entry is None:
            return {'status': 'WAITING_FOR_V43_DISPOSITION'}
        status = entry.get('status')
        if status == 'NO_SIGNAL_WITHIN_4H':
            return {'status': 'NO_SIGNAL', 'entry_reference_usdt': None}
        if status != 'ENTRY_REFERENCE_OBSERVED':
            return {'status': 'UNAVAILABLE_V43_ENTRY'}
        try:
            t = parse_time(entry['entry_first_seen_utc']);bar = parse_time(entry['entry_spot_bar_end_utc'])
        except (KeyError, TypeError, ValueError):
            return {'status': 'UNAVAILABLE_V43_ENTRY'}
        px = number(entry.get('entry_reference_price_usdt'))
        if not px or t <= start or bar <= start or bar > t:
            return {'status': 'UNAVAILABLE_V43_ENTRY'}
        return {'status': 'ENTERED', 'entry_seen_utc': iso(t), 'entry_price_bar_end_utc':iso(bar),
                'entry_reference_usdt': px, 'source': 'V4.3', 'signal_id':entry.get('signal_id')}
    signals = sorted((d for d in v44_decisions if d.get('symbol') == symbol and
        d.get('linked_v4_3_cohort_id') == cid and d.get('entry_signal') is True and
        start < parse_time(d['decision_recorded_utc']) <= start+ENTRY_SIGNAL_WINDOW),
        key=lambda x: x['decision_recorded_utc'])
    if not signals:
        # A signal with no link can occur when V4.4 ran before a V4.3 cohort
        # was committed. Never mislabel it as a verified NO_SIGNAL/cash return.
        unlinked = any(d.get('symbol') == symbol and d.get('entry_signal') is True
            and d.get('linked_v4_3_cohort_id') != cid and
            start < parse_time(d['decision_recorded_utc']) <= start+ENTRY_SIGNAL_WINDOW
            for d in v44_decisions)
        if unlinked:
            return {'status':'AMBIGUOUS_UNLINKED_RECOVERY'}
    if signals:
        signal = signals[0]
        recorded = parse_time(signal['decision_recorded_utc'])
        entry = next((e for e in v44_entries if e.get('decision_id') == signal['decision_id']), None)
        if entry is None:
            return {'status':'WAITING_FOR_V44_ENTRY', 'signal_id':signal['decision_id']}
        if entry.get('status') != 'REFERENCE_ENTRY_OBSERVED':
            return {'status':'UNAVAILABLE_V44_ENTRY'}
        try:
            t=parse_time(entry['entry_seen_utc']);bar=parse_time(entry['entry_price_bar_end_utc'])
        except (KeyError, TypeError, ValueError):
            return {'status':'UNAVAILABLE_V44_ENTRY'}
        px=number(entry.get('entry_reference_usdt'))
        if not px or t <= recorded or bar <= recorded or bar > t:
            return {'status':'UNAVAILABLE_V44_ENTRY'}
        return {'status':'ENTERED', 'entry_seen_utc':iso(t),'entry_price_bar_end_utc':iso(bar),
                'entry_reference_usdt':px,'source':'V4.4','signal_id':signal['decision_id']}
    return {'status':coverage_status(v44_decisions, symbol, start)}


def arm_mark(entry, mark, snapshots, now, common_target=None):
    """No entry -> cash; late entry -> still cash at common event horizon."""
    status = entry['status']; end = parse_time(mark['first_seen_utc']); end_px=number(mark['reference_close_usdt'])
    if status in ('NO_SIGNAL','OBSERVED_NO_SPOT_RECOVERY'):
        return {'status':'CASH_NO_SIGNAL', 'net_pct_by_cost':{str(c):0.0 for c in COSTS},
                'sampled_mae_pct':0.0, 'entry_delay_min':None}
    if status != 'ENTERED':
        return {'status':'UNAVAILABLE_'+status, 'net_pct_by_cost':None,
                'sampled_mae_pct':None, 'entry_delay_min':None}
    t=parse_time(entry['entry_seen_utc']);p=entry['entry_reference_usdt']
    if t > end or (common_target is not None and t >= common_target):
        return {'status':'CASH_ENTRY_AFTER_HORIZON', 'net_pct_by_cost':{str(c):0.0 for c in COSTS},
                'sampled_mae_pct':0.0, 'entry_delay_min':None}
    if not p or p <= 0:
        return {'status':'UNAVAILABLE_ENTRY_PRICE','net_pct_by_cost':None,
                'sampled_mae_pct':None,'entry_delay_min':None}
    gross = 100 * (end_px/p-1)
    sampled = [number(s['reference_close_usdt']) for s in snapshots
               if s['symbol']==mark['symbol'] and t<=parse_time(s['first_seen_utc'])<=end]
    sampled = [v for v in sampled if v is not None and v>0]
    if not sampled:
        return {'status':'UNAVAILABLE_INTERMEDIATE_MARKS','net_pct_by_cost':None,
                'sampled_mae_pct':None,'entry_delay_min':None}
    return {'status':'REFERENCE_HOLD_TO_COMMON_CLOCK',
            'net_pct_by_cost':{str(c):round(gross-c,5) for c in COSTS},
            'gross_pct':round(gross,5),
            'sampled_mae_pct':round(min(0.0,100*(min([p]+sampled)/p-1)),5),
            'entry_delay_min':None}


def summarize(reports, now):
    result={}
    for sym in ('BTC','ETH'):
        block={}
        for h in HORIZONS:
            episodes=[r for r in reports if r.get('symbol')==sym and r.get('horizon_h')==h]
            comparable=[r for r in episodes if r['status']=='EVALUATED' and
                r['arms']['DIP_NEXT']['net_pct_by_cost'] is not None]
            arms={}
            for arm in ARMS:
                pairs=[r for r in comparable if r['arms'][arm]['net_pct_by_cost'] is not None]
                delta=[r['arms'][arm]['net_pct_by_cost']['0.14'] - r['arms']['DIP_NEXT']['net_pct_by_cost']['0.14'] for r in pairs]
                returns=[r['arms'][arm]['net_pct_by_cost']['0.14'] for r in pairs]
                delays=[r['arms'][arm]['entry_delay_min'] for r in pairs if r['arms'][arm]['entry_delay_min'] is not None]
                arms[arm]={'paired_episodes':len(pairs),
                    'mean_common_end_net_pct':round(mean(returns),5) if returns else None,
                    'mean_delta_vs_dip_pp':round(mean(delta),5) if delta else None,
                    'improved_share_pct':round(100*sum(x>0 for x in delta)/len(delta),2) if delta else None,
                    'no_trade_count':sum(r['arms'][arm]['status'].startswith('CASH') for r in pairs),
                    'mean_wait_to_entry_min':round(mean(delays),2) if delays else None,
                    'missed_profitable_dip_count':sum(r['arms'][arm]['status'].startswith('CASH') and r['arms']['DIP_NEXT']['net_pct_by_cost']['0.14']>0 for r in pairs),
                    'avoided_losing_dip_count':sum(r['arms'][arm]['status'].startswith('CASH') and r['arms']['DIP_NEXT']['net_pct_by_cost']['0.14']<0 for r in pairs),
                    'mean_sampled_mae_pct':round(mean([r['arms'][arm]['sampled_mae_pct'] for r in pairs if r['arms'][arm]['sampled_mae_pct'] is not None]),5) if any(r['arms'][arm]['sampled_mae_pct'] is not None for r in pairs) else None}
            block[str(h)+'h']={'registered_final_episodes':len(episodes), 'baseline_evaluable':len(comparable), 'arms':arms}
        result[sym]=block
    return {'version':VERSION,'asof_utc':iso(now),'research_only':True,
        'orders_enabled':False,'buy_alerts_enabled':False,'status':'PROSPECTIVE_MONITORING',
        'by_symbol':result,
        'interpretation': 'Common cohort-clock price comparisons with missed opportunity counted as zero cash return; sampled spot closes and illustrative roundtrip costs only.',
        'limitations':['V4.3 and V4.4 entry rules were historically informed; new observations only after V4.5 inception.',
                       'Different holding durations, funding, leverage, adverse intrabar excursion and executable fills NOT modeled.',
                       'Cash zero assumes no interest; do not treat missing data as cash.',
                       'Comparisons conditional on cohort completeness; report observation coverage separately.',
                       'Sampled MAE uses saved 5-minute closes at collector intervals, not candle lows.',
                       'No statistical inference until enough independent mature cohorts; no buy alerts or orders.']}


def evaluate(now,root):
    now=now.astimezone(UTC);root=Path(root);ledger=root/'buy_edge_v4_5';ledger.mkdir(parents=True,exist_ok=True)
    protocol=ledger/'protocol.json'
    if not protocol.exists():
        protocol.write_text(json.dumps({'version':VERSION,'inception_utc':iso(now),
            'no_historical_backfill':True,'locked_common_horizons_h':list(HORIZONS),
            'round_trip_cost_scenarios_pct':list(COSTS),'cash_policy':'zero_when_verified_no_signal'},indent=2)+'\n',encoding='utf8')
        report=summarize([],now);report['status']='BOOTSTRAPPED_NO_HISTORICAL_BACKFILL';report['new_final_evaluations']=0
        (ledger/'latest.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n',encoding='utf8')
        return report
    cfg=json.loads(protocol.read_text(encoding='utf8'))
    if (cfg.get('version')!=VERSION or cfg.get('no_historical_backfill') is not True or
        cfg.get('locked_common_horizons_h')!=list(HORIZONS)):
        raise ValueError('Locked V4.5 protocol mismatch: refuse re-interpretation')
    inception=parse_time(cfg['inception_utc'])
    cohorts=load_rows(root,'comparison_v4_3/cohorts',now)
    entries43=load_rows(root,'comparison_v4_3/entries',now)
    decisions44=load_rows(root,'market_state_v4_4/decisions',now)
    entries44=load_rows(root,'market_state_v4_4/entries',now)
    snaps=load_rows(root,'snapshots',now)
    snaps=sorted((s for s in snaps if spot_usable(s,now)),key=lambda s:(s['first_seen_utc'],s['snapshot_id']))
    registered=load_rows(ledger,'evaluations',now)
    known={r['evaluation_id'] for r in registered}
    count=0;unresolved=0
    for c in sorted(cohorts,key=lambda c:(c.get('start_seen_utc',''),c.get('symbol',''))):
        if c.get('symbol') not in ('BTC','ETH'):continue
        try:start=parse_time(c['start_seen_utc'])
        except (KeyError,ValueError,TypeError):continue
        if start<=inception or start>now:continue
        for h in HORIZONS:
            k=hashlib.sha256((VERSION+'|'+c['cohort_id']+'|'+str(h)).encode()).hexdigest()[:24]
            if k in known:continue
            target=start+timedelta(hours=h)
            if now<=target+PRICE_WINDOW:continue
            mark=common_mark(snaps,c['symbol'],target,now)
            arms={arm:resolved_arm(c,arm,entries43,decisions44,entries44) for arm in ARMS}
            valid_statuses={'ENTERED','NO_SIGNAL','OBSERVED_NO_SPOT_RECOVERY',
                            'UNAVAILABLE_V43_ENTRY','UNAVAILABLE_V44_ENTRY'}
            complete=all(a['status'] in valid_statuses for a in arms.values())
            if not complete and now<start+FORCE_FINALIZE:
                unresolved+=1;continue
            if mark is None and now<start+FORCE_FINALIZE:
                unresolved+=1;continue
            measured={a:arm_mark(e,mark,snaps,now,target) if mark else {'status':'NO_COMMON_TERMINAL_MARK',
                'net_pct_by_cost':None,'sampled_mae_pct':None,'entry_delay_min':None} for a,e in arms.items()}
            for a,e in arms.items():
                if (mark and e['status']=='ENTERED' and measured[a]['status']=='REFERENCE_HOLD_TO_COMMON_CLOCK'):
                    measured[a]['entry_delay_min']=round((parse_time(e['entry_seen_utc'])-start).total_seconds()/60,2)
            # Absence of upstream data is never a no-signal / zero-return observation.
            record={'evaluation_id':k,'version':VERSION,'cohort_id':c['cohort_id'],'symbol':c['symbol'],
                    'cohort_start_seen_utc':c['start_seen_utc'],'horizon_h':h,'common_due_utc':iso(target),
                    'common_mark_seen_utc':mark['first_seen_utc'] if mark else None,
                    'common_mark_bar_end_utc':mark['spot_bar_end_utc'] if mark else None,
                    'common_mark_price_usdt':mark['reference_close_usdt'] if mark else None,
                    'status':'EVALUATED' if mark and complete else 'UNAVAILABLE_INCOMPLETE_INPUT',
                    'arms':{a:{**measured[a],'source_disposition':arms[a]['status'],
                          'entry_first_seen_utc':arms[a].get('entry_seen_utc'),
                          'entry_reference_usdt':arms[a].get('entry_reference_usdt')}
                          for a in ARMS},'completed_utc':iso(now),'research_only':True}
            if append_once(ledger,'evaluations',now.date().isoformat(),record,'evaluation_id'):
                count+=1;registered.append(record);known.add(k)
    report=summarize(registered,now)
    report.update({'inception_utc':cfg['inception_utc'],'new_final_evaluations':count,
                   'open_due_evaluations':unresolved,'final_evaluations_total':len(registered)})
    (ledger/'latest.json').write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf8')
    return report


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path('data/v4_monitor'))
    a=p.parse_args();r=evaluate(datetime.now(UTC),a.root)
    print('V4_5_BUY_EDGE '+json.dumps({'status':r['status'],'new_final_evaluations':r['new_final_evaluations'],
        'by_symbol':{k:{h:v['baseline_evaluable'] for h,v in block.items()} for k,block in r['by_symbol'].items()}}))

if __name__=='__main__':
    main()
