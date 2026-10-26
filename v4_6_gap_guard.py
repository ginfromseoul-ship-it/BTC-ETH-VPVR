#!/usr/bin/env python3
"""V4.6 collector gap guard (observation rescue; no trading or synthetic data).

The script reads committed source timestamps and actual GitHub workflow runs,
then optionally DISPATCHES the missing collector. It never rewrites V4 ledgers,
invents historical observations, or suppresses the V4.4 90-minute safety rule.

Run with --dry-run for diagnostic mode. Workflow handles credentials in GH_TOKEN.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

UTC = timezone.utc
VERSION = 'v4_6_gap_guard_2026-10-26_01'
SOURCES = {
    'spot': {'workflow': 'market_flow.yml', 'file': 'data/market_flow_latest.json', 'timestamp': 'collected_utc'},
    'derivatives': {'workflow': 'coinalyze.yml', 'file': 'data/coinalyze_derivatives_latest.json', 'timestamp': 'generated_utc'},
}
MAX_AGE_MIN = 22   # Collector-generation age threshold
MAX_BAR_AGE_MIN = 25  # Underlying completed candle/flow age threshold
MAX_CROSS_BAR_SKEW_MIN = 12  # Catch asynchrony before V4 rejects >15min skew
MANUAL_COOLDOWN_MIN = 22
MAX_MANUAL_DISPATCHES_HOUR = 2
RECENT_SUCCEEDED_GRACE_MIN = 5
ACTIVE_STATUSES = frozenset(('queued', 'in_progress', 'pending', 'waiting', 'requested'))


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError('timestamp missing or not a string')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timestamp missing timezone')
    return result.astimezone(UTC)


def iso(t):
    return t.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def source_state(kind, payload, now):
    """Only a genuine, timestamped, healthy collector output counts as fresh."""
    stamp = SOURCES[kind]['timestamp']
    if not isinstance(payload, dict):
        return {'state': 'INVALID', 'reason': 'file_missing_or_unreadable', 'age_minutes': None}
    try:
        age = (now - parse_time(payload.get(stamp))).total_seconds() / 60
    except (ValueError, TypeError):
        return {'state': 'INVALID', 'reason': 'invalid_source_timestamp', 'age_minutes': None}
    if age < -2:
        return {'state': 'INVALID', 'reason': 'future_source_timestamp', 'age_minutes': round(age, 2)}
    if age > MAX_AGE_MIN:
        return {'state': 'STALE', 'reason': 'timestamp_age_exceeded', 'age_minutes': round(age, 2)}
    assets = payload.get('assets') or {}
    symbols = ('BTCUSDT', 'ETHUSDT') if kind == 'spot' else ('BTC', 'ETH')
    required = ('spot_flow',) if kind == 'spot' else ('open_interest', 'futures_flow', 'liquidations')
    bar_times=[]
    for symbol in symbols:
        asset = assets.get(symbol) or {}
        quality = asset.get('quality') or {}
        if any(quality.get(k) != 'ok' for k in required):
            return {'state': 'QUALITY_FAIL', 'reason': f'{symbol}_core_feed_unavailable', 'age_minutes': round(age, 2)}
        stamp = ((asset.get('spot') or {}).get('flow') or {}).get('last_closed_bar_utc') if kind == 'spot' else \
                ((asset.get('futures_flow') or {}).get('last_complete_utc'))
        try:
            t = parse_time(stamp)
        except (TypeError, ValueError):
            return {'state': 'INVALID', 'reason': f'{symbol}_missing_completed_bar_time', 'age_minutes': round(age,2)}
        bar_times.append(t)
    oldest_bar=min(bar_times)
    bar_age=(now-oldest_bar).total_seconds()/60
    if bar_age < -2:
        return {'state': 'INVALID', 'reason': 'future_completed_bar_time', 'age_minutes': round(age,2)}
    if bar_age > MAX_BAR_AGE_MIN:
        return {'state':'STALE_BAR','reason':'completed_bar_too_old',
                'age_minutes':round(age,2),'bar_age_minutes':round(bar_age,2),'bar_end_utc':iso(oldest_bar)}
    return {'state': 'FRESH', 'reason': 'collector_and_completed_bars_current',
            'age_minutes': round(age, 2), 'bar_age_minutes':round(bar_age,2),
            'bar_end_utc': iso(oldest_bar)}


def evaluate_dispatch(source, runs, now):
    """Plan only: never calls GitHub, deterministic for independent testing."""
    state = source['state']
    if state == 'FRESH':
        return 'NO_ACTION_FRESH'
    if runs is None or not isinstance(runs, list):
        return 'BLOCKED_RUN_HISTORY_UNAVAILABLE'
    recent = []
    for r in runs:
        try:
            when = parse_time(r['created_at'])
        except (KeyError, TypeError, ValueError):
            return 'BLOCKED_INVALID_RUN_HISTORY'
        age = (now - when).total_seconds() / 60
        if age < -2:
            return 'BLOCKED_FUTURE_RUN_HISTORY'
        if age <= 70:
            recent.append((age, r))
    # Consider all run statuses, including a pending scheduled run.
    if any(r.get('status') in ACTIVE_STATUSES for _, r in recent):
        return 'WAIT_ACTIVE_SOURCE_RUN'
    if any(age <= RECENT_SUCCEEDED_GRACE_MIN and r.get('conclusion') == 'success' for age, r in recent):
        return 'WAIT_RECENT_SUCCESS_TO_PERSIST'
    manuals = [age for age, r in recent if r.get('event') == 'workflow_dispatch']
    if any(age < MANUAL_COOLDOWN_MIN for age in manuals):
        return 'WAIT_MANUAL_COOLDOWN'
    if sum(age <= 60 for age in manuals) >= MAX_MANUAL_DISPATCHES_HOUR:
        return 'BLOCKED_HOURLY_DISPATCH_LIMIT'
    return 'DISPATCH'


def recent_gap_metrics(root, now):
    """Descriptive first-seen coverage, NEVER a synthetic reconstructed history."""
    cutoff = now - timedelta(hours=24)
    rows = []
    for day in (now.date()-timedelta(days=1), now.date()):
        path = Path(root) / 'snapshots' / (day.isoformat()+'.jsonl')
        if not path.exists():
            continue
        for ln in path.read_text(encoding='utf8').splitlines():
            if not ln.strip():
                continue
            try:
                x=json.loads(ln)
                dt=parse_time(x['first_seen_utc'])
            except (KeyError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if cutoff <= dt <= now:
                rows.append((dt,x))
    result={}
    for symbol in ('BTC','ETH'):
        items = sorted([(t,x) for t,x in rows if x.get('symbol')==symbol],key=lambda v:v[0])
        valid=[t for t,x in items if x.get('state')!='DATA_UNAVAILABLE' and not x.get('missing')]
        gaps=[round((b-a).total_seconds()/60,2) for a,b in zip(valid,valid[1:])]
        result[symbol] = {
            'observations': len(items),
            'full_feature_verified': len(valid),
            'source_unverified': len(items)-len(valid),
            'verified_pair_gaps_over_90min': sum(g>90 for g in gaps),
            'largest_verified_pair_gap_min': max(gaps) if gaps else None,
            'last_verified_age_min': round((now-valid[-1]).total_seconds()/60,2) if valid else None,
            'interpretation': 'Recorded observer snapshots, not exchange trades or missing backfill',
        }
    return result


class GithubAPI:
    def __init__(self, token, repo):
        if not token or not repo or '/' not in repo:
            raise ValueError('GH_TOKEN and GITHUB_REPOSITORY required')
        self.token, self.repo = token, repo

    def request(self, method, path, body=None):
        base='https://api.github.com/repos/'+self.repo+'/actions/workflows/'+path
        headers={
            'Accept': 'application/vnd.github+json',
            'Authorization': 'Bearer '+self.token,
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'btc-eth-v46-gap-guard',
        }
        data=None
        if body is not None:
            data=json.dumps(body).encode('utf8')
            headers['Content-Type']='application/json'
        req=Request(base,data=data,headers=headers,method=method)
        try:
            with urlopen(req,timeout=15) as conn:
                content=conn.read()
                return json.loads(content) if content else {}
        except (HTTPError, URLError, TimeoutError) as ex:
            raise RuntimeError(f'GitHub {method} {path.split("?")[0]}: {type(ex).__name__}') from ex

    def runs(self, workflow):
        q=urlencode({'per_page':75,'branch':'main'})
        obj=self.request('GET',f'{workflow}/runs?{q}')
        if not isinstance(obj,dict) or not isinstance(obj.get('workflow_runs'),list):
            raise ValueError('GitHub workflow runs response incomplete')
        return obj['workflow_runs']

    def dispatch(self, workflow):
        self.request('POST',f'{workflow}/dispatches',{'ref':'main'})


def load(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf8'))
    except (OSError, ValueError):
        return None


def run(now, root, api, dry_run):
    # Read ALL run histories before dispatching ANY source. No partial state if API fails.
    histories={key:api.runs(meta['workflow']) for key,meta in SOURCES.items()}
    sources={key:source_state(key,load(Path(root)/meta['file']),now) for key,meta in SOURCES.items()}
    # A spot update can arrive while futures bars are still ~18m behind. Even
    # individually fresh feeds are not pair-usable when completed bars diverge.
    if all(sources[k]['state']=='FRESH' for k in SOURCES):
        a=parse_time(sources['spot']['bar_end_utc'])
        b=parse_time(sources['derivatives']['bar_end_utc'])
        if abs((a-b).total_seconds()) > MAX_CROSS_BAR_SKEW_MIN*60:
            older='spot' if a<b else 'derivatives'
            sources[older]['state']='SKEWED'
            sources[older]['reason']='cross_source_completed_bar_skew'
            sources[older]['cross_source_skew_minutes']=round(abs((a-b).total_seconds())/60,2)
    decisions={key:evaluate_dispatch(sources[key],histories[key],now) for key in SOURCES}
    results={}
    for key,choice in decisions.items():
        if choice!='DISPATCH':
            results[key]=choice
        elif dry_run:
            results[key]='WOULD_DISPATCH_DRY_RUN'
        else:
            api.dispatch(SOURCES[key]['workflow'])
            results[key]='DISPATCHED'
    return {
        'version': VERSION, 'checked_utc': iso(now), 'research_only': True,
        'orders_enabled': False, 'synthetic_snapshots_created': False,
        'dry_run': dry_run, 'sources': sources, 'plans': decisions, 'actions': results,
        'snapshot_quality_24h': recent_gap_metrics(Path(root)/'data/v4_monitor',now),
        'limits': {'max_age_minutes':MAX_AGE_MIN,'max_bar_age_minutes':MAX_BAR_AGE_MIN,
                   'max_cross_bar_skew_minutes':MAX_CROSS_BAR_SKEW_MIN,
                   'manual_cooldown_minutes':MANUAL_COOLDOWN_MIN,
                    'max_dispatches_per_hour_per_source':MAX_MANUAL_DISPATCHES_HOUR},
        'limitations': ['GitHub schedules are best-effort; no guaranteed 15-minute arrival',
                        'No retrospective synthetic V4 snapshots or false first-seen timestamps',
                        'Dispatch API success means queued, NOT that collection has succeeded',
                        'V4.4 90-minute baseline integrity check remains unchanged'],
    }


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,default=Path('.'))
    p.add_argument('--dry-run',action='store_true',help='Plan without dispatching workflows')
    p.add_argument('--output',type=Path,default=Path('/tmp/v4_6_gap_guard_audit.json'))
    args=p.parse_args()
    api=GithubAPI(os.environ.get('GH_TOKEN'),os.environ.get('GITHUB_REPOSITORY'))
    report=run(datetime.now(UTC),args.root,api,args.dry_run)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf8')
    print('V4_6_GAP_GUARD '+json.dumps({'at':report['checked_utc'],'plans':report['plans'],
                                      'actions':report['actions'],'quality':report['snapshot_quality_24h']}))
    summary=os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with Path(summary).open('a',encoding='utf8') as f:
            f.write('### V4.6 collector gap guard\n\n')
            f.write('| Source | Age (min) | State | Decision | Result |\n|---|---:|---|---|---|\n')
            for s in SOURCES:
                d=report['sources'][s]
                f.write(f'| {s} | {d["age_minutes"]} | {d["state"]} | {report["plans"][s]} | {report["actions"][s]} |\n')
            f.write('\nNo synthetic data, trading or changes to existing V4 ledgers.\n')


if __name__=='__main__':
    main()
