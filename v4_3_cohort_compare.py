#!/usr/bin/env python3
"""V4.3 prospective buy-timing comparisons. PAPER ONLY, not trading signals.

The three arms (same ex ante shock episode):
  DIP_NEXT:    first subsequent observed spot mark after a 4h <= -1% drop.
  WATCH_NEXT:  first subsequent observed mark after REVERSAL_WATCH or CANDIDATE.
  CONFIRM_NEXT:first subsequent observed mark after PAPER_CANDIDATE_UNVALIDATED.

New protocol starts empty on FIRST successful run; it does not reinterpret older
snapshots as prospective decisions. All marks use only records first observed
before each decision and only later recorded spot closes for proxy entry/exit.
One cohort per symbol per 24h, even when no delayed confirmation occurs.

Stores append-only daily jsonl of cohorts, arm dispositions and 1h/4h/24h marks.
No exchange API, no key, no orders, no actionable buy alerts.
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
VERSION = 'v4_3_cohort_comparison_2026-10-26_locked_01'
ARMS = ('DIP_NEXT', 'WATCH_NEXT', 'CONFIRM_NEXT')
HOURS = (1, 4, 24)
COSTS = (0.14, 0.22, 0.35)   # illustrative round-trip spot-equivalent %; no actual fills
COOLDOWN = timedelta(hours=24)
WAIT_FOR_CONFIRMATION = timedelta(hours=4)
SIGNAL_GRACE = timedelta(minutes=35)  # allow delayed collectors to publish valid candles
ENTRY_MAX_WAIT = timedelta(minutes=45)
OUTCOME_MAX_WAIT = timedelta(minutes=45)
MAX_SPOT_BAR_AGE = timedelta(minutes=35)
SHOCK_4H_PCT = -1.0 # pre-registered research gate, not fitted to outcomes


def parse_time(s):
    if not isinstance(s, str):
        raise ValueError('UTC time is required')
    d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    if d.tzinfo is None: raise ValueError('naive datetime not allowed')
    return d.astimezone(UTC)


def iso(d):return d.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def numeric(x):
    if isinstance(x, bool) or x is None:return None
    try:
        y = float(x)
        return y if math.isfinite(y) else None
    except (ValueError, TypeError):return None


def jsonl(path):
    if not path.is_file():return []
    return [json.loads(s) for s in path.read_text('utf8').splitlines() if s.strip()]


def day_path(base, kind, t):
    return base/kind/(parse_time(t).date().isoformat()+'.jsonl')


def append_once(path, row, id_key):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same-day records are small; duplicate protection survives action retries.
    if any(r.get(id_key) == row[id_key] for r in jsonl(path)):
        return False
    with path.open('a', encoding='utf8') as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False)+'\n')
    return True


def recent_rows(base, kind, now, days=5):
    rows=[]
    for i in range(days+1):
        t = iso(now-timedelta(days=i))
        rows.extend(jsonl(day_path(base,kind,t)))
    return rows


def observed_snapshot(x, asof):
    if x.get('symbol') not in ('BTC','ETH'):return False
    if x.get('spot_price_observation_usable') is not True:return False
    price=numeric(x.get('reference_close_usdt'))
    if price is None or price<=0:return False
    try:
        first=parse_time(x['first_seen_utc']);bar=parse_time(x['spot_bar_end_utc'])
    except (ValueError,KeyError,TypeError):return False
    if first>asof or bar>first or first-bar>MAX_SPOT_BAR_AGE:return False
    return True


def snapshots_from_v4(base, asof):
    out = []
    for i in range(5+1):
        p=base/'snapshots'/(asof.date()-timedelta(days=i)).isoformat()
        p=p.with_suffix('.jsonl')
        out.extend(jsonl(p))
    seen={}
    for s in out:
        if not isinstance(s,dict) or not s.get('snapshot_id'):continue
        if observed_snapshot(s, asof):
            prev=seen.get(s['snapshot_id'])
            if prev is None or s['first_seen_utc']<prev['first_seen_utc']:
                seen[s['snapshot_id']]=s
    return sorted(seen.values(),key=lambda x:(x['first_seen_utc'],x['symbol'],x['snapshot_id']))


def shock_pct(s):
    return numeric(s.get('evidence',{}).get('spot_price_change_4h_pct'))


def is_cohort_start(s):
    drop=shock_pct(s)
    return drop is not None and drop<=SHOCK_4H_PCT


def arm_signal(cohort,arm,snapshots):
    start=parse_time(cohort['start_seen_utc'])
    if arm=='DIP_NEXT':
        return next((x for x in snapshots if x['snapshot_id']==cohort['origin_snapshot_id']),None)
    candidates=[]
    for s in snapshots:
        if s['symbol']!=cohort['symbol']:continue
        t=parse_time(s['first_seen_utc'])
        if not start<=t<=start+WAIT_FOR_CONFIRMATION:continue
        state=s.get('state')
        if arm=='WATCH_NEXT' and state in ('REVERSAL_WATCH','PAPER_CANDIDATE_UNVALIDATED'):
            candidates.append(s)
        if arm=='CONFIRM_NEXT' and state=='PAPER_CANDIDATE_UNVALIDATED':
            candidates.append(s)
    return candidates[0] if candidates else None


def next_entry(signal, snaps):
    first=parse_time(signal['first_seen_utc']);symbol=signal['symbol']
    return next((x for x in snaps if x['symbol']==symbol
                 and first<parse_time(x['first_seen_utc'])<=first+ENTRY_MAX_WAIT
                 and parse_time(x['spot_bar_end_utc'])>first),None)


def exit_mark(entry, horizon, snapshots):
    t=parse_time(entry['entry_first_seen_utc'])+timedelta(hours=horizon)
    return next((s for s in snapshots if s['symbol']==entry['symbol']
                 and t<=parse_time(s['first_seen_utc'])<=t+OUTCOME_MAX_WAIT
                 and parse_time(s['spot_bar_end_utc'])>=t),None)


def roundmean(vals):return round(mean(vals),5) if vals else None


def summary(cohorts, entries, outcomes, now):
    by_entry={(x['cohort_id'],x['arm']):x for x in entries}
    by_result={(x['cohort_id'],x['arm'],x['hours']):x for x in outcomes}
    blocks={}
    for sym in ('BTC','ETH'):
        sc=[c for c in cohorts if c['symbol']==sym]
        arms={}
        for arm in ARMS:
            es=[by_entry[(c['cohort_id'],arm)] for c in sc if (c['cohort_id'],arm) in by_entry]
            opened=[e for e in es if e['status']=='ENTRY_REFERENCE_OBSERVED']
            settled=[by_result.get((e['cohort_id'],arm,24)) for e in opened]
            settled=[x for x in settled if x and x['status']=='OUTCOME_REFERENCE_OBSERVED']
            arms[arm]={'resolved_arm_count':len(es),'entered':len(opened),
                       'skipped_no_signal':sum(x['status']=='NO_SIGNAL_WITHIN_4H' for x in es),
                       'unavailable_entry':sum(x['status']=='NO_TIMELY_POST_SIGNAL_PRICE' for x in es),
                       '24h_outcomes':len(settled),
                       'mean_24h_net_0_14pct':roundmean([x['net_pct_by_cost']['0.14'] for x in settled])}
        paired={}
        for arm in ('WATCH_NEXT','CONFIRM_NEXT'):
            pairs=[]
            for c in sc:
                a=by_result.get((c['cohort_id'],'DIP_NEXT',24));b=by_result.get((c['cohort_id'],arm,24))
                if a and b and a['status']==b['status']=='OUTCOME_REFERENCE_OBSERVED':
                    pairs.append(b['net_pct_by_cost']['0.14']-a['net_pct_by_cost']['0.14'])
            # Missing confirmation is an outcome, not a deleted observation.
            # Report how the immediate-dip benchmark behaved on those same skipped episodes.
            baseline_on_skips=[]
            for c in sc:
                arm_entry=by_entry.get((c['cohort_id'],arm))
                baseline=by_result.get((c['cohort_id'],'DIP_NEXT',24))
                if arm_entry and arm_entry['status']=='NO_SIGNAL_WITHIN_4H' and baseline and baseline['status']=='OUTCOME_REFERENCE_OBSERVED':
                    baseline_on_skips.append(baseline['net_pct_by_cost']['0.14'])
            paired[arm]={'matched_cohorts':len(pairs),'mean_net_24h_difference_vs_dip_pp':roundmean(pairs),
                         'skipped_confirmations_with_baseline_24h':len(baseline_on_skips),
                         'baseline_24h_mean_net_on_no_signal_cohorts':roundmean(baseline_on_skips),
                         'baseline_profitable_pct_on_no_signal_cohorts':round(100*sum(x>0 for x in baseline_on_skips)/len(baseline_on_skips),2) if baseline_on_skips else None,
                         'limitation':'Entry times differ; this is not a same-clock liquidation/stop-adjusted P&L comparison.'}
        blocks[sym]={'cohorts_started':len(sc),'by_arm':arms,'paired_diagnostics':paired}
    return {'version':VERSION,'generated_utc':iso(now),'protocol':'forward_only_from_bootstrap',
      'research_only':True,'orders_enabled':False,'buy_alerts_enabled':False,
      'comparison_kind':'Same ex-ante downtrend cohort; three predefined decision policies',
      'horizons_h':list(HOURS),'cost_scenarios_pct':list(COSTS),
      'by_symbol':blocks,'limitations':[
        'Prospective observations only: existing V4 historical snapshots pre-bootstrap are never reclassified as decisions.',
        'All reference prices are observed Binance spot 5m closes, NOT executable futures fills.',
        'No intrabar risk, liquidation, funding, stop slippage, brokerage execution or leverage simulation.',
        'Signal detection and entry require separate recorded snapshots; missing timely snapshots are not fabricated.',
        'Baseline has no signal filter; watch and confirm must be counted as NO_SIGNAL when not triggered.',
        'Per-arm 24h outcomes follow their own entry timestamps; paired differences are diagnostic not risk-adjusted alpha.',
        'V4 thresholds were informed by previous history; only genuinely new observations test new predictive value.',
        'No live orders, no actionable buy alerts, no assumption that orderbook snapshot demonstrates absorption.']}


def evaluate(now, root, bootstrap_if_missing=True):
    root=Path(root)
    now=now.astimezone(UTC)
    ledger=root/'comparison_v4_3'
    ledger.mkdir(parents=True,exist_ok=True)
    cfg=ledger/'protocol.json'
    if not cfg.exists():
        if not bootstrap_if_missing:raise ValueError('Protocol must be bootstrapped first')
        # Critical: protects the first run from relabeling prior V4 data as an ex-ante experiment.
        cfg.write_text(json.dumps({'version':VERSION,'inception_utc':iso(now),
               'historical_snapshots_excluded':True},indent=2)+'\n',encoding='utf8')
        r=summary([],[],[],now)
        r['status']='BOOTSTRAPPED_NO_HISTORICAL_BACKFILL'
        (ledger/'latest.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        return r
    config=json.loads(cfg.read_text('utf8'))
    if config.get('version')!=VERSION or not config.get('historical_snapshots_excluded'):
        raise ValueError('Immutable protocol mismatch; do not silently reset')
    inception=parse_time(config['inception_utc'])
    snaps=snapshots_from_v4(root,now)
    cohorts=recent_rows(ledger,'cohorts',now)
    cohorts.sort(key=lambda x:(x['start_seen_utc'],x['symbol']))
    seen_ids={c['cohort_id'] for c in cohorts}
    new_cohorts=0
    for s in snaps:
        first=parse_time(s['first_seen_utc'])
        if first<=inception or not is_cohort_start(s):continue
        recent=max((parse_time(c['start_seen_utc']) for c in cohorts if c['symbol']==s['symbol']
                    and parse_time(c['start_seen_utc'])<=first),default=None)
        if recent is not None and first-recent<COOLDOWN:continue
        cohort_id=hashlib.sha256((VERSION+'|'+s['symbol']+'|'+s['snapshot_id']).encode()).hexdigest()[:24]
        if cohort_id in seen_ids:continue
        record={'cohort_id':cohort_id,'symbol':s['symbol'],'origin_snapshot_id':s['snapshot_id'],
                'start_seen_utc':s['first_seen_utc'],'origin_spot_bar_end_utc':s['spot_bar_end_utc'],
                'observed_drop_4h_pct':shock_pct(s),'reference_close_usdt':numeric(s['reference_close_usdt']),
                'version':VERSION}
        if append_once(day_path(ledger,'cohorts',s['first_seen_utc']),record,'cohort_id'):
            cohorts.append(record);seen_ids.add(cohort_id);new_cohorts+=1
    cohorts.sort(key=lambda x:(x['start_seen_utc'],x['symbol']))
    entries=recent_rows(ledger,'entries',now)
    entry_ids={(e['cohort_id'],e['arm']) for e in entries}
    new_entries=0
    for c in cohorts:
        start=parse_time(c['start_seen_utc'])
        for arm in ARMS:
            key=(c['cohort_id'],arm)
            if key in entry_ids:continue
            signal=arm_signal(c,arm,snaps)
            if signal is None:
                if arm!='DIP_NEXT' and now>start+WAIT_FOR_CONFIRMATION+SIGNAL_GRACE:
                    status='NO_SIGNAL_WITHIN_4H'
                else:continue
                entry=None
            else:
                entry=next_entry(signal,snaps)
                if entry is None:
                    if now<=parse_time(signal['first_seen_utc'])+ENTRY_MAX_WAIT:continue
                    status='NO_TIMELY_POST_SIGNAL_PRICE'
                else:status='ENTRY_REFERENCE_OBSERVED'
            record={'entry_id':c['cohort_id']+'|'+arm,'cohort_id':c['cohort_id'],
                'symbol':c['symbol'],'arm':arm,'status':status,'recorded_utc':iso(now),
                'signal_id':signal['snapshot_id'] if signal else None,
                'signal_first_seen_utc':signal['first_seen_utc'] if signal else None,
                'entry_first_seen_utc':entry['first_seen_utc'] if entry else None,
                'entry_spot_bar_end_utc':entry['spot_bar_end_utc'] if entry else None,
                'entry_reference_price_usdt':numeric(entry['reference_close_usdt']) if entry else None,
                'entry_method':'first independently observed future 5m spot close; not a fill',
                'version':VERSION}
            if append_once(day_path(ledger,'entries',iso(now)),record,'entry_id'):
                entries.append(record);entry_ids.add(key);new_entries+=1
    outcomes=recent_rows(ledger,'outcomes',now)
    result_ids={o['outcome_id'] for o in outcomes}
    new_outcomes=0
    for e in entries:
        if e['status']!='ENTRY_REFERENCE_OBSERVED':continue
        ent=parse_time(e['entry_first_seen_utc']);px=numeric(e['entry_reference_price_usdt'])
        if px is None or px<=0:continue
        for h in HOURS:
            oid=e['entry_id']+'|'+str(h)+'h'
            if oid in result_ids:continue
            target=ent+timedelta(hours=h)
            mark=exit_mark(e,h,snaps)
            if mark is None:
                if now<=target+OUTCOME_MAX_WAIT:continue
                status='UNAVAILABLE_NO_TIMELY_FORWARD_PRICE'
            else:status='OUTCOME_REFERENCE_OBSERVED'
            gross=100*(numeric(mark['reference_close_usdt'])/px-1) if mark else None
            record={'outcome_id':oid,'entry_id':e['entry_id'],'cohort_id':e['cohort_id'],
                'symbol':e['symbol'],'arm':e['arm'],'hours':h,'due_utc':iso(target),
                'recorded_utc':iso(now),'status':status,
                'price_seen_utc':mark['first_seen_utc'] if mark else None,
                'price_bar_end_utc':mark['spot_bar_end_utc'] if mark else None,
                'exit_reference_price_usdt':numeric(mark['reference_close_usdt']) if mark else None,
                'gross_pct':round(gross,5) if gross is not None else None,
                'net_pct_by_cost':{str(x):round(gross-x,5) for x in COSTS} if gross is not None else None,
                'reference_only':True,'version':VERSION}
            if append_once(day_path(ledger,'outcomes',iso(now)),record,'outcome_id'):
                outcomes.append(record);result_ids.add(oid);new_outcomes+=1
    report=summary(cohorts,entries,outcomes,now)
    report.update({'status':'PROSPECTIVE_RESEARCH_ACTIVE','inception_utc':config['inception_utc'],
        'new_cohorts':new_cohorts,'new_arm_dispositions':new_entries,'new_outcomes':new_outcomes,
        'observation_snapshots_scanned':len(snaps)})
    (ledger/'latest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')
    return report


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',default='data/v4_monitor',type=Path)
    a=p.parse_args()
    r=evaluate(datetime.now(UTC),a.root)
    print('V4_3_COMPARISON '+json.dumps({'status':r['status'],
        'new_cohorts':r.get('new_cohorts',0),'new_dispositions':r.get('new_arm_dispositions',0),
        'new_outcomes':r.get('new_outcomes',0),
        'cohorts':{s:j['cohorts_started'] for s,j in r['by_symbol'].items()}}))

if __name__=='__main__':main()
