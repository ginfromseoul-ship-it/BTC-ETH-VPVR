#!/usr/bin/env python3
"""V4.2 source-cadence and prospective monitor operational quality audit.

Runs after the unchanged V4.1 observer. No market orders, no buy alerts,
no retrospective conversion of late data into tradeable entries.
The GitHub scheduled-start coverage is APPROXIMATE, not exact slot matching.
"""
from __future__ import annotations
import argparse
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
VERSION = 'v4_2_operational_2026-10-26_01'
SOURCE_NAMES = {'spot': 'BTC ETH Market Flow 15min', 'derivatives': 'BTC ETH Coinalyze Derivatives'}
EXPECTED_PER_24H = 96  # 4 cron slots/hour, not a guarantee of actual start.
EXCLUDE_RECENT_MIN = 90  # scheduled runs can be late: do not count recent slots as missed.
SCHEDULE_WINDOW_HOURS = 24
WARN_SCHEDULE_COVERAGE = 75  # percent, operational attention threshold only.
FRESH_MAX_MIN = 35
BAR_SKEW_MAX_MIN = 15


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
    """Accepted: GH API response object, --paginate --slurp page array, or None."""
    if payload is None:
        return None
    pages = payload if isinstance(payload, list) else [payload]
    if not pages or any(not isinstance(p, dict) or not isinstance(p.get('workflow_runs'), list) for p in pages):
        return None
    by_id = {}
    for page in pages:
        for run in page['workflow_runs']:
            if isinstance(run, dict) and run.get('id') is not None:
                by_id[str(run['id'])] = run
    return list(by_id.values())


def cadence_summary(payload, now, workflow_name):
    """Count scheduled runs in a mature 24h window, not a perfect per-slot audit.

    Includes only event=schedule; workflow_dispatch and workflow_run do not
    count as scheduled starts. `created_at` is run creation, NOT due cron time.
    """
    rows = flatten_run_pages(payload)
    if rows is None:
        return {'status': 'API_UNAVAILABLE', 'method': 'GitHub Actions recent schedule event read unavailable',
                'expected_slots': EXPECTED_PER_24H, 'observed_scheduled_starts': None,
                'approximate_start_coverage_pct': None, 'median_start_interval_min': None,
                'largest_start_interval_min': None}
    end = now - timedelta(minutes=EXCLUDE_RECENT_MIN)
    start = end - timedelta(hours=SCHEDULE_WINDOW_HOURS)
    eligible = []
    for r in rows:
        if r.get('event') != 'schedule' or r.get('name') != workflow_name:
            continue
        try:
            t = parse_time(r['created_at'])
        except (ValueError, TypeError, KeyError):
            continue
        if start <= t < end:
            eligible.append((t, r))
    eligible.sort(key=lambda v: v[0])
    # Oldest page must reach the beginning of the window. Otherwise truncate,
    # never misrepresent a partial API page as missing scheduled starts.
    all_times = []
    for r in rows:
        try:
            all_times.append(parse_time(r['created_at']))
        except (ValueError, TypeError, KeyError):
            pass
    page_complete = bool(all_times) and min(all_times) <= start
    if not page_complete:
        return {'status': 'INSUFFICIENT_API_HISTORY', 'observed_window_start_utc': iso(start),
                'observed_window_end_utc': iso(end), 'api_oldest_run_utc': iso(min(all_times)) if all_times else None,
                'expected_slots': EXPECTED_PER_24H, 'observed_scheduled_starts': None,
                'approximate_start_coverage_pct': None,
                'median_start_interval_min': None, 'largest_start_interval_min': None}
    observed = len(eligible)
    intervals = [(b[0]-a[0]).total_seconds()/60 for a,b in zip(eligible, eligible[1:])]
    srt = sorted(intervals)
    median_interval = srt[len(srt)//2] if srt else None
    success = sum(r.get('conclusion') == 'success' for _,r in eligible)
    failures = sum(r.get('conclusion') == 'failure' for _,r in eligible)
    complete = sum(r.get('status') == 'completed' for _,r in eligible)
    coverage = round2(100*observed/EXPECTED_PER_24H)
    return {'status': 'LOW_SCHEDULE_START_COVERAGE' if coverage < WARN_SCHEDULE_COVERAGE else 'OK',
            'observed_window_start_utc': iso(start), 'observed_window_end_utc': iso(end),
            'expected_slots': EXPECTED_PER_24H, 'observed_scheduled_starts': observed,
            'approximate_start_coverage_pct': coverage,
            'scheduled_completed': complete, 'scheduled_success': success,
            'scheduled_failure': failures,
            'median_start_interval_min': round2(median_interval) if median_interval is not None else None,
            'largest_start_interval_min': round2(max(intervals)) if intervals else None,
            'method': 'Actual event=schedule runs created in a 24h window ending 90m ago; approximate count; no exact cron slot matching; historical schedule assumed unchanged.'}


def individual_health(latest, now, symbol):
    x = latest.get('states', {}).get(symbol, {})
    spot_end = x.get('spot_bar_end_utc')
    future_end = x.get('futures_bar_end_utc')
    source_ages = latest.get('input_sources', {})
    spot_age = age_min(now, spot_end)
    future_age = age_min(now, future_end)
    collected_age = age_min(now, source_ages.get('spot_collected_utc'))
    deriv_age = age_min(now, source_ages.get('futures_collected_utc'))
    try:
        skew = round2(abs((parse_time(spot_end)-parse_time(future_end)).total_seconds())/60)
    except (ValueError, TypeError, OverflowError):
        skew = None
    checks = {
        'spot_candle_fresh': spot_age is not None and -1 <= spot_age <= FRESH_MAX_MIN,
        'futures_candle_fresh': future_age is not None and -1 <= future_age <= FRESH_MAX_MIN,
        'spot_collector_fresh': collected_age is not None and -1 <= collected_age <= FRESH_MAX_MIN,
        'derivatives_collector_fresh': deriv_age is not None and -1 <= deriv_age <= FRESH_MAX_MIN,
        'cross_source_bar_skew_ok': skew is not None and skew <= BAR_SKEW_MAX_MIN,
        'usable_model_decision': x.get('state') not in (None, 'DATA_UNAVAILABLE') and not x.get('missing'),
    }
    return {'status': 'FRESH_AND_USABLE' if all(checks.values()) else 'BLOCKED_OR_STALE',
            'model_state': x.get('state'), 'spot_bar_age_minutes': spot_age,
            'futures_bar_age_minutes': future_age,
            'spot_collector_age_minutes': collected_age,
            'derivatives_collector_age_minutes': deriv_age,
            'cross_source_end_skew_minutes': skew,
            'failed_checks': [k for k,v in checks.items() if not v]}


def append_unique(path, item):
    path.parent.mkdir(parents=True, exist_ok=True)
    history = []
    if path.is_file():
        history = [json.loads(s) for s in path.read_text('utf8').splitlines() if s.strip()]
    if any(x.get('audit_run_id') == item['audit_run_id'] for x in history):
        return False
    with path.open('a', encoding='utf8') as f:
        f.write(json.dumps(item, ensure_ascii=False, allow_nan=False, sort_keys=True)+'\n')
    return True


def audit(latest, spot_runs, future_runs, now, out, run_id, event_name='local', upstream=None):
    """Operational metrics only. Does NOT modify signal, entry or outcome rules."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    source_info = {
        'spot': cadence_summary(spot_runs, now, SOURCE_NAMES['spot']),
        'derivatives': cadence_summary(future_runs, now, SOURCE_NAMES['derivatives'])}
    assets = {s: individual_health(latest,now,s) for s in ('BTC','ETH')}
    status = ('BLOCKED' if any(v['status']!='FRESH_AND_USABLE' for v in assets.values())
              else 'DEGRADED_SCHEDULE' if any(v['status']=='LOW_SCHEDULE_START_COVERAGE' for v in source_info.values())
              else 'SOURCE_CADENCE_UNVERIFIED' if any(v['status']!='OK' for v in source_info.values())
              else 'OPERATING_NORMALLY')
    data = {'version': VERSION,'audit_utc': iso(now),'status': status,
            'audit_run_id': str(run_id),'trigger': {'event_name': event_name,
               'upstream_workflow': upstream or None},
            'v4_monitor_generated_utc': latest.get('generated_utc'),
            'observer_version': latest.get('version'),
            'new_snapshots_saved': latest.get('new_snapshots_saved'),
            'new_paper_entries':latest.get('new_paper_entries'),
            'new_forward_outcomes':latest.get('new_forward_outcomes'),
            'sources':source_info,'assets': assets,
            'research_only': True,'real_money_trading_enabled':False,
            'interpretation': 'Operations and freshness audit, NOT evidence that buy signals are profitable.',
            'limitations': ['GitHub scheduler can delay/skip runs; event-trigger does not fix missing upstream data',
                            'Schedule coverage counts created scheduled runs, not exact on-time slot matching',
                            'Price/volume inputs are asynchronous aggregated feeds, not tick-level order book',
                            'No new paper candidate is not a system fault; inspect freshness separately']}
    (out/'operational_health.json').write_text(json.dumps(data,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf8')
    hist = out/'run_audits'/f'{now.date().isoformat()}.jsonl'
    append_unique(hist, {k:v for k,v in data.items() if k not in ('limitations',)},)
    return data


def read_file(path):
    if not path or not Path(path).is_file():return None
    try:
        return json.loads(Path(path).read_text('utf8'))
    except (OSError, json.JSONDecodeError):return None


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
    print('V4_2_OPERATIONAL '+json.dumps({'status':report['status'],
          'sources': {k:x.get('approximate_start_coverage_pct') for k,x in report['sources'].items()},
          'assets': {k:x['status'] for k,x in report['assets'].items()},
          'new_snapshots':report['new_snapshots_saved'],'new_entries':report['new_paper_entries']}))

if __name__=='__main__':main()
