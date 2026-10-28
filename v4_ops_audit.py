#!/usr/bin/env python3
"""BTC/ETH research operational audit: read-only coverage of prospective source observations.

Compatible with V4.1/V4.2 invocation and V4.12/V4.13 research ledgers. This
is solely a health audit. No trade orders, alerts, retrospective fills, or
changes to an existing research hypothesis. No dependence on future data.

The distinction that matters:
    - SCHEDULE starts: GitHub cron activity only (can be low despite Cloudflare).
    - OBSERVED spot bins: actual usable first-seen spot research snapshots.
    - OBSERVED structure bins: usable spot plus causal derivative evidence.

A snapshot bin is evidence that the prospective research monitor observed the
required data at least once. It is NOT proof that a raw exchange collector ran
exactly at the slot boundary, that order-book absorption occurred, or that a
trade could have executed at the recorded candle close.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
VERSION = 'v4_2_operational_observed_coverage_2026-10-29_01'
SOURCE_NAMES = {'spot': 'BTC ETH Market Flow 15min', 'derivatives': 'BTC ETH Coinalyze Derivatives'}
EXPECTED_PER_24H = 96
EXCLUDE_RECENT_MIN = 90
SCHEDULE_WINDOW_HOURS = 24
WARN_SCHEDULE_COVERAGE = 75
FRESH_MAX_MIN = 35
BAR_SKEW_MAX_MIN = 15
COVERAGE_SLOT_MIN = 15
COVERAGE_WINDOW_H = 24
COVERAGE_MATURITY_MIN = 15
SPOT_COVERAGE_GOAL_PCT = 95.0
STRUCTURE_COVERAGE_GOAL_PCT = 95.0


def parse_time(s):
    if not isinstance(s, str):
        raise ValueError('not ISO timestamp')
    dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('missing timezone')
    return dt.astimezone(UTC)


def iso(t):
    return t.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def round2(x):
    return round(x, 2)


def age_min(now, ts):
    try:
        d = (now - parse_time(ts)).total_seconds() / 60
        return round2(d) if math.isfinite(d) else None
    except (ValueError, TypeError, OverflowError):
        return None


def flatten_run_pages(payload):
    """Accept GitHub workflow runs response, paginated page array, or None."""
    if payload is None:
        return None
    pages = payload if isinstance(payload, list) else [payload]
    if not pages or any(not isinstance(p, dict) or not isinstance(p.get('workflow_runs'), list)
                        for p in pages):
        return None
    by_id = {}
    for page in pages:
        for run in page['workflow_runs']:
            if isinstance(run, dict) and run.get('id') is not None:
                by_id[str(run['id'])] = run
    return list(by_id.values())


def cadence_summary(payload, now, workflow_name):
    """Legacy schedule-only diagnostic; intentionally NOT source-data coverage."""
    rows = flatten_run_pages(payload)
    if rows is None:
        return {'status':'API_UNAVAILABLE','method':'GitHub Actions API unavailable',
                'expected_slots':EXPECTED_PER_24H,'observed_scheduled_starts':None,
                'approximate_start_coverage_pct':None,'median_start_interval_min':None,
                'largest_start_interval_min':None}
    end = now - timedelta(minutes=EXCLUDE_RECENT_MIN)
    start = end - timedelta(hours=SCHEDULE_WINDOW_HOURS)
    eligible=[]
    for r in rows:
        if r.get('event')!='schedule' or r.get('name')!=workflow_name:
            continue
        try:
            t=parse_time(r['created_at'])
        except (ValueError,TypeError,KeyError):
            continue
        if start<=t<end:
            eligible.append((t,r))
    eligible.sort(key=lambda x:x[0])
    all_times=[]
    for r in rows:
        try:all_times.append(parse_time(r['created_at']))
        except (ValueError,TypeError,KeyError):pass
    if not all_times or min(all_times)>start:
        return {'status':'INSUFFICIENT_API_HISTORY','observed_window_start_utc':iso(start),
                'observed_window_end_utc':iso(end),'api_oldest_run_utc':iso(min(all_times)) if all_times else None,
                'expected_slots':EXPECTED_PER_24H,'observed_scheduled_starts':None,
                'approximate_start_coverage_pct':None,
                'median_start_interval_min':None,'largest_start_interval_min':None}
    intervals=[(b[0]-a[0]).total_seconds()/60 for a,b in zip(eligible,eligible[1:])]
    sorted_intervals=sorted(intervals)
    observed=len(eligible)
    coverage=round2(100*observed/EXPECTED_PER_24H)
    return {'status':'LOW_SCHEDULE_START_COVERAGE' if coverage<WARN_SCHEDULE_COVERAGE else 'OK',
            'observed_window_start_utc':iso(start),'observed_window_end_utc':iso(end),
            'expected_slots':EXPECTED_PER_24H,'observed_scheduled_starts':observed,
            'approximate_start_coverage_pct':coverage,
            'scheduled_completed':sum(r.get('status')=='completed' for _,r in eligible),
            'scheduled_success':sum(r.get('conclusion')=='success' for _,r in eligible),
            'scheduled_failure':sum(r.get('conclusion')=='failure' for _,r in eligible),
            'median_start_interval_min':round2(sorted_intervals[len(sorted_intervals)//2]) if intervals else None,
            'largest_start_interval_min':round2(max(intervals)) if intervals else None,
            'method':'Actual event=schedule runs created in 24h ending 90m ago; excludes external workflow_dispatch and retries. NOT a data coverage rate.'}


def individual_health(latest,now,symbol):
    x=latest.get('states',{}).get(symbol,{})
    spot_end=x.get('spot_bar_end_utc')
    future_end=x.get('futures_bar_end_utc')
    sources=latest.get('input_sources',{})
    spot_age=age_min(now,spot_end)
    future_age=age_min(now,future_end)
    collected_age=age_min(now,sources.get('spot_collected_utc'))
    deriv_age=age_min(now,sources.get('futures_collected_utc'))
    try:skew=round2(abs((parse_time(spot_end)-parse_time(future_end)).total_seconds())/60)
    except (ValueError,TypeError,OverflowError):skew=None
    checks={
        'spot_candle_fresh':spot_age is not None and -1<=spot_age<=FRESH_MAX_MIN,
        'futures_candle_fresh':future_age is not None and -1<=future_age<=FRESH_MAX_MIN,
        'spot_collector_fresh':collected_age is not None and -1<=collected_age<=FRESH_MAX_MIN,
        'derivatives_collector_fresh':deriv_age is not None and -1<=deriv_age<=FRESH_MAX_MIN,
        'cross_source_bar_skew_ok':skew is not None and skew<=BAR_SKEW_MAX_MIN,
        'usable_model_decision':x.get('state') not in (None,'DATA_UNAVAILABLE') and not x.get('missing')}
    return {'status':'FRESH_AND_USABLE' if all(checks.values()) else 'BLOCKED_OR_STALE',
            'model_state':x.get('state'),'spot_bar_age_minutes':spot_age,
            'futures_bar_age_minutes':future_age,
            'spot_collector_age_minutes':collected_age,
            'derivatives_collector_age_minutes':deriv_age,
            'cross_source_end_skew_minutes':skew,
            'failed_checks':[k for k,v in checks.items() if not v]}


def _finite(value,low=None,high=None):
    try:
        x=float(value)
        return math.isfinite(x) and (low is None or x>=low) and (high is None or x<=high)
    except (TypeError,ValueError,OverflowError):
        return False


def _observation_flags(row):
    """Causal validity at first_seen; never reconstruct a missing source later."""
    try:
        seen=parse_time(row['first_seen_utc'])
        spot_end=parse_time(row['spot_bar_end_utc'])
        fut_end=parse_time(row['futures_bar_end_utc'])
    except (KeyError,ValueError,TypeError,OverflowError):
        return False,False
    evidence=row.get('evidence')
    if not isinstance(evidence,dict):
        return False,False
    spot_valid=(row.get('spot_price_observation_usable') is True and
        row.get('research_only') is True and row.get('not_an_order_or_buy_recommendation') is True and
        _finite(row.get('reference_close_usdt'),0.000000001) and
        _finite(evidence.get('spot_buy_share_1h_pct'),0,100) and
        _finite(evidence.get('spot_buy_share_last15m_pct'),0,100) and
        _finite(evidence.get('spot_price_change_4h_pct')) and
        timedelta(0)<=seen-spot_end<=timedelta(minutes=FRESH_MAX_MIN))
    structure_valid=(spot_valid and
        timedelta(0)<=seen-fut_end<=timedelta(minutes=FRESH_MAX_MIN) and
        abs((spot_end-fut_end).total_seconds())<=BAR_SKEW_MAX_MIN*60 and
        isinstance(row.get('missing'),list) and not row['missing'] and
        row.get('state') not in (None,'DATA_UNAVAILABLE') and
        _finite(evidence.get('futures_buy_share_1h_pct'),0,100) and
        _finite(evidence.get('oi_native_change_1h_pct')) and
        _finite(evidence.get('reported_long_liquidations_1h_usd'),0) and
        _finite(evidence.get('reported_short_liquidations_1h_usd'),0) and
        _finite(evidence.get('depth_bid_ask_ratio_0_1pct_snapshot'),0))
    return bool(spot_valid),bool(structure_valid)


def observation_coverage(snapshot_dir,now):
    """Count *distinct observed* 15-minute bins within a mature 24h window.

    - Reads only pre-existing snapshots; never backfills or alters them.
    - Deduplicates repeated snapshots and retries by first_seen time bin.
    - Source availability at the later audit time does not upgrade old evidence.
    - A partial/inaugural snapshot ledger is UNVERIFIED, not zero coverage.
    """
    slot_sec=COVERAGE_SLOT_MIN*60
    mature=now-timedelta(minutes=COVERAGE_MATURITY_MIN)
    end=datetime.fromtimestamp((int(mature.timestamp())//slot_sec)*slot_sec,tz=UTC)
    start=end-timedelta(hours=COVERAGE_WINDOW_H)
    size=int(COVERAGE_WINDOW_H*60/COVERAGE_SLOT_MIN)
    directory=Path(snapshot_dir)
    out={'version':'observation_bins_20261029_01','status':'UNVERIFIED',
         'window_start_utc':iso(start),'window_end_utc':iso(end),
         'maturity_minutes':COVERAGE_MATURITY_MIN,'slots_expected':size,
         'goals_pct':{'spot':SPOT_COVERAGE_GOAL_PCT,'market_structure':STRUCTURE_COVERAGE_GOAL_PCT},
         'by_symbol':{},'method':'At least one causally valid V4.1 prospective snapshot first-seen in each fixed UTC 15m bin; not exchange ticks, executable prices, or completed shock outcomes.'}
    if not directory.is_dir():
        out['reason']='SNAPSHOT_DIRECTORY_MISSING'
        return out
    # Limit disk reads to the relevant observation dates; the ledger grows
    # indefinitely, but a 24h operational audit must remain O(24h).
    first_day=(start-timedelta(days=1)).date()
    last_day=end.date()
    day=first_day
    files=[]
    while day<=last_day:
        candidate=directory/(day.isoformat()+'.jsonl')
        if candidate.is_file():files.append(candidate)
        day+=timedelta(days=1)
    if not files:
        out['reason']='SNAPSHOT_HISTORY_MISSING'
        return out
    bins={s:{'any':set(),'spot':set(),'structure':set(),'missing_codes_by_bin':{}} for s in ('BTC','ETH')}
    earliest=None
    row_count=0
    invalid_rows=0
    older_seen=False
    for f in files:
        # All dates may contain a prior timestamp; use actual first_seen for window inclusion.
        # Files after window end cannot contribute (but can exist if a later run backdates).
        try:lines=f.read_text(encoding='utf8').splitlines()
        except OSError:
            out['reason']='SNAPSHOT_READ_FAILURE'
            return out
        for line in lines:
            if not line.strip():continue
            try:
                r=json.loads(line)
                if not isinstance(r,dict):raise ValueError('not an object')
                t=parse_time(r['first_seen_utc'])
            except (json.JSONDecodeError,ValueError,TypeError,KeyError,OverflowError):
                invalid_rows+=1
                continue
            if earliest is None or t<earliest:earliest=t
            if t<start:older_seen=True
            if not start<=t<end:continue
            if r.get('symbol') not in bins:
                invalid_rows+=1
                continue
            row_count+=1
            slot=int((t-start).total_seconds()//slot_sec)
            binset=bins[r['symbol']]
            binset['any'].add(slot)
            spot_ok,structure_ok=_observation_flags(r)
            if spot_ok:binset['spot'].add(slot)
            if structure_ok:
                binset['structure'].add(slot)
            else:
                codes=binset['missing_codes_by_bin'].setdefault(slot,set())
                for code in (r.get('missing') if isinstance(r.get('missing'),list) else []):
                    if isinstance(code,str):codes.add(code)
                if not codes:
                    codes.add('OTHER_CAUSAL_FIELD_OR_FRESHNESS_FAILURE')
    out['rows_in_window']=row_count
    out['malformed_rows']=invalid_rows
    out['older_history_present']=older_seen
    # Prevent newly installed research from being called low coverage during the
    # first 24h of collection. A snapshot in the first bin is also acceptable.
    enough_history=(older_seen or bool(bins['BTC']['any'] & {0}) and bool(bins['ETH']['any'] & {0}))
    if not enough_history:
        out['reason']='INSUFFICIENT_24H_LEDGER_HISTORY'
    elif invalid_rows:
        out['reason']='MALFORMED_SNAPSHOT_RECORDS'
    else:
        out['status']='VERIFIED'
    for symbol in ('BTC','ETH'):
        x=bins[symbol]
        reason_counts={}
        for bin_no,codes in x['missing_codes_by_bin'].items():
            # A later valid first-seen snapshot in the *same* 15m bin repairs
            # the health metric for that bin, but never changes old evidence.
            if bin_no in x['structure']:continue
            for code in codes:
                reason_counts[code]=reason_counts.get(code,0)+1
        result={
          'observed_bins':len(x['any']),
          'spot_valid_bins':len(x['spot']),
          'structure_valid_bins':len(x['structure']),
          'spot_coverage_pct':round2(100*len(x['spot'])/size),
          'structure_coverage_pct':round2(100*len(x['structure'])/size),
          'missing_spot_bins':size-len(x['spot']),
          'missing_structure_bins':size-len(x['structure']),
          'structure_gap_reason_bin_counts':dict(sorted(reason_counts.items())),
          'last_spot_valid_bin_utc':iso(start+timedelta(minutes=COVERAGE_SLOT_MIN*max(x['spot']))) if x['spot'] else None,
          'last_structure_valid_bin_utc':iso(start+timedelta(minutes=COVERAGE_SLOT_MIN*max(x['structure']))) if x['structure'] else None,
          'structure_ready_is_not_absorption_proof':True}
        out['by_symbol'][symbol]=result
    return out


def append_unique(path,item):
    path.parent.mkdir(parents=True,exist_ok=True)
    history=[]
    if path.is_file():
        history=[json.loads(s) for s in path.read_text('utf8').splitlines() if s.strip()]
    if any(x.get('audit_run_id')==item['audit_run_id'] for x in history):
        return False
    with path.open('a',encoding='utf8') as f:
        f.write(json.dumps(item,ensure_ascii=False,allow_nan=False,sort_keys=True)+'\n')
    return True


def audit(latest,spot_runs,future_runs,now,out,run_id,event_name='local',upstream=None):
    """Operational metrics only: preserves signal, episode, and trade rules."""
    out=Path(out)
    out.mkdir(parents=True,exist_ok=True)
    source_info={
      'spot':cadence_summary(spot_runs,now,SOURCE_NAMES['spot']),
      'derivatives':cadence_summary(future_runs,now,SOURCE_NAMES['derivatives'])}
    assets={s:individual_health(latest,now,s) for s in ('BTC','ETH')}
    coverage=observation_coverage(out/'snapshots',now)
    if any(v['status']!='FRESH_AND_USABLE' for v in assets.values()):
        status='BLOCKED'
    elif coverage.get('reason') in ('MALFORMED_SNAPSHOT_RECORDS','SNAPSHOT_READ_FAILURE'):
        # A corrupt prospective ledger is not the same as missing GitHub cron
        # starts and must never be hidden behind a schedule diagnostic.
        status='OBSERVATION_LEDGER_INVALID'
    elif coverage['status']=='VERIFIED':
        results=list(coverage['by_symbol'].values())
        if any(v['spot_coverage_pct']<SPOT_COVERAGE_GOAL_PCT for v in results):
            status='DEGRADED_SPOT_OBSERVATIONS'
        elif any(v['structure_coverage_pct']<STRUCTURE_COVERAGE_GOAL_PCT for v in results):
            status='DEGRADED_STRUCTURE_OBSERVATIONS'
        else:
            status='OPERATING_NORMALLY'
    else:
        # Older tests and initial deployment remain compatible. Explicitly mark
        # this as schedule-based fallback, not verified observation coverage.
        status=('DEGRADED_SCHEDULE' if any(v['status']=='LOW_SCHEDULE_START_COVERAGE' for v in source_info.values())
                else 'SOURCE_CADENCE_UNVERIFIED' if any(v['status']!='OK' for v in source_info.values())
                else 'OPERATING_NORMALLY')
    data={'version':VERSION,'audit_utc':iso(now),'status':status,'audit_run_id':str(run_id),
          'trigger':{'event_name':event_name,'upstream_workflow':upstream or None},
          'v4_monitor_generated_utc':latest.get('generated_utc'),
          'observer_version':latest.get('version'),
          'new_snapshots_saved':latest.get('new_snapshots_saved'),
          'new_paper_entries':latest.get('new_paper_entries'),
          'new_forward_outcomes':latest.get('new_forward_outcomes'),
          'sources':source_info,'assets':assets,
          'observed_data_coverage':coverage,
          'coverage_is_actual_research_snapshot_bins':True,
          'scheduled_start_rate_is_only_a_diagnostic':True,
          'research_only':True,'real_money_trading_enabled':False,
          'interpretation':'End-to-end research evidence coverage and source health, NOT profitable entry signals.',
          'limitations':[
             'Coverage counts V4.1 first-seen research snapshots, not raw exchange-source uptime or exact cron punctuality',
             'GitHub schedule only counts scheduled starts; Cloudflare workflow_dispatch and retries are separate',
             'Valid structure observations are not physical market absorption or sell-exhaustion proof',
             'Snapshots have asynchronous spot and futures aggregation and are not executable fills',
             'No paper-entry condition, research cohort, or signal threshold was changed']}
    (out/'operational_health.json').write_text(json.dumps(data,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf8')
    append_unique(out/'run_audits'/f'{now.date().isoformat()}.jsonl',
                  {k:v for k,v in data.items() if k not in ('limitations',)})
    return data


def read_file(path):
    if not path or not Path(path).is_file():return None
    try:return json.loads(Path(path).read_text('utf8'))
    except (OSError,json.JSONDecodeError):return None


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--latest',default='data/v4_monitor/latest.json')
    p.add_argument('--spot-runs')
    p.add_argument('--derivatives-runs')
    p.add_argument('--out',default='data/v4_monitor')
    p.add_argument('--audit-run-id',default=(os.environ['GITHUB_RUN_ID']+'-'+os.environ.get('GITHUB_RUN_ATTEMPT','1')) if os.environ.get('GITHUB_RUN_ID') else None)
    p.add_argument('--event-name',default=os.environ.get('GITHUB_EVENT_NAME','local'))
    p.add_argument('--upstream',default=os.environ.get('V4_UPSTREAM_WORKFLOW'))
    a=p.parse_args()
    now=datetime.now(UTC)
    data=read_file(a.latest)
    if not isinstance(data,dict):raise SystemExit('V4 observer JSON missing; audit cannot proceed')
    report=audit(data,read_file(a.spot_runs),read_file(a.derivatives_runs),now,a.out,
                 a.audit_run_id or iso(now),a.event_name,a.upstream)
    print('V4_2_OPERATIONAL '+json.dumps({
         'status':report['status'],
         'schedule_only_pct':{k:x.get('approximate_start_coverage_pct') for k,x in report['sources'].items()},
         'actual_coverage_status':report['observed_data_coverage']['status'],
         'actual_bin_coverage':{k:{'spot_pct':v['spot_coverage_pct'],
                                    'structure_pct':v['structure_coverage_pct']}
                                for k,v in report['observed_data_coverage']['by_symbol'].items()},
         'assets':{k:x['status'] for k,x in report['assets'].items()},
         'new_snapshots':report['new_snapshots_saved'],
         'new_entries':report['new_paper_entries']},ensure_ascii=False))

if __name__=='__main__':main()
