#!/usr/bin/env python3
"""BTC/ETH V3 buy-timing RESEARCH, immutable decision logic 2026-10-26.

No trading, no retrospective tuning, no historical liquidation claims.
- Event: first 4h futures dip <= -1.5% BTC / -2% ETH with 24h event cooldown.
- Decision: exactly +24 closed five-minute bars (2h after shock close).
- Simulated entry: +5m processing buffer, then following candle open (i+26).
- Missing OI fails closed for OI-confirmed states; quote taker flow is an aggregate proxy.
- Historical 24m results are DEVELOPMENTAL/RE-USED, not true unseen holdout.
"""
from __future__ import annotations
import argparse, csv, hashlib, json, math, sqlite3, statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

SHOCK={'BTC':-0.015,'ETH':-0.020}
HORIZONS={'1h':12,'4h':48,'24h':288}
COSTS=(0.14,0.22,0.35)  # percent, round trip; not broker fee claims
COOLDOWN=86400
WAIT=24 # 2 hours = 24 five-minute bars
ENTRY_WAIT=2 # two bars after anchor+2h, i.e. 5m after decision-bar close
OI_GUARD=300
PRICE_FLOOR_BUFFER=.001
MAX_STRUCTURAL_RISK_PCT=3.0
MODEL_VERSION='v3_buy_timing_2026-10-26_locked_01'
REQUIRED={'t','end_t','future_close','future_open','spot_taker_delta_quote_usdt','futures_taker_delta_native','binance_oi_native','binance_oi_observed_t'}


def iso(t):return datetime.fromtimestamp(t,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
def mean(v):return round(statistics.mean(v),4) if v else None
def median(v):return round(statistics.median(v),4) if v else None


def prefix(rows,col):
    o=[0.]
    for r in rows:o.append(o[-1]+float(r[col] or 0))
    return o


def ratio(v,d,start,end):
    volume=v[end+1]-v[start]
    return (d[end+1]-d[start])/volume if volume>1e-9 else None


def calc_features(rows,i,j,p):
    # Row fields: t, end_t, futures open, close, spot delta, futures delta,
    # spot quoted volume, futures native volume, OI native, OI observation t, high, low
    s=ratio(p['sv'],p['sd'],j-11,j)
    prev=ratio(p['sv'],p['sd'],j-23,j-12)
    last15=ratio(p['sv'],p['sd'],j-2,j)
    f=ratio(p['fv'],p['fd'],j-11,j)
    oi_values=[rows[k][8] for k in (j-48,j-12,j)]
    oi_times=[rows[k][9] for k in (j-48,j-12,j)]
    oi_ok=all(v is not None and math.isfinite(v) and v>0 for v in oi_values)
    oi_ok=oi_ok and all(t is not None and t+OI_GUARD<=rows[k][1] for t,k in zip(oi_times,(j-48,j-12,j)))
    oi_4h=(oi_values[-1]/oi_values[0]-1) if oi_ok else None
    oi_1h=(oi_values[-1]/oi_values[1]-1) if oi_ok else None
    low=min(r[11] for r in rows[i:j+1])
    close=rows[j][3]
    floor=low*(1-PRICE_FLOOR_BUFFER)
    riskpct=100*(close-floor)/close if close>0 else None
    return {'price_reclaim':close>=rows[i][3],
            'spot_1h_imbalance':s,'spot_prior_1h_imbalance':prev,
            'spot_improvement_pp':100*(s-prev) if s is not None and prev is not None else None,
            'spot_last15_imbalance':last15,
            'futures_1h_imbalance':f,
            'oi_valid':oi_ok,'oi_1h_change_pct':100*oi_1h if oi_ok else None,
            'oi_4h_change_pct':100*oi_4h if oi_ok else None,
            'shock_close':rows[i][3], 'decision_close':close,
            'structural_low':low,'invalidation_price':floor,
            'risk_to_invalidation_pct_at_decision':riskpct}


def classify(f):
    # Pre-registered, causally knowable conditions. Not fitted to 24m returns.
    if any(f[k] is None for k in ('spot_1h_imbalance','spot_prior_1h_imbalance','spot_last15_imbalance')):
        return 'NO_DATA'
    close_confirm=(f['price_reclaim'] and f['spot_1h_imbalance']>0 and f['spot_last15_imbalance']>0)
    improvement=f['spot_improvement_pp']>=2.0
    oi_stable=f['oi_valid'] and f['oi_1h_change_pct']>=-.1
    risk_ok=f['risk_to_invalidation_pct_at_decision'] is not None and 0<f['risk_to_invalidation_pct_at_decision']<=MAX_STRUCTURAL_RISK_PCT
    if close_confirm and improvement and oi_stable and risk_ok:
        return 'BUY_SETUP_STRICT'
    if close_confirm and risk_ok:
        return 'BUY_SETUP_EARLY'
    if (f['price_reclaim'] or f['spot_1h_imbalance']>0 or f['spot_last15_imbalance']>0):
        return 'WATCH_NO_BUY'
    return 'SELLING_NO_BUY'


def statistics_for(events):
    if not events:return {'n':0, 'win_24h_pct':None, 'mean_24h_net_pct':None,'median_24h_net_pct':None,
                          'mean_1h_net_pct':None,'mean_4h_net_pct':None,'mean_mae_24h_pct':None,'mean_mfe_24h_pct':None,
                          'mean_24h_net_0_22pct_cost':None,'mean_24h_net_0_35pct_cost':None,
                          'median_invalidation_risk_pct':None,'invalidation_hit_24h_pct':None,'two_R_first_hit_24h_pct':None}
    return {'n':len(events),'win_24h_pct':round(100*sum(r['net_24h']>0 for r in events)/len(events),2),
            'mean_24h_net_pct':mean([r['net_24h'] for r in events]),
            'median_24h_net_pct':median([r['net_24h'] for r in events]),
            'mean_1h_net_pct':mean([r['net_1h'] for r in events]),
            'mean_4h_net_pct':mean([r['net_4h'] for r in events]),
            'mean_mae_24h_pct':mean([r['mae_24h'] for r in events]),
            'mean_mfe_24h_pct':mean([r['mfe_24h'] for r in events]),
            'mean_24h_net_0_22pct_cost':mean([r['net_24h']-.08 for r in events]),
            'mean_24h_net_0_35pct_cost':mean([r['net_24h']-.21 for r in events]),
            'median_invalidation_risk_pct':median([r['decision_invalidation_risk_pct'] for r in events]),
            'invalidation_hit_24h_pct':round(100*sum(r['stop_hit_24h'] for r in events)/len(events),2),
            'two_R_first_hit_24h_pct':round(100*sum(r['first_hit_24h']=='target_2R' for r in events)/len(events),2)}


def first_hit(rows,entry_i,stop,target):
    # Any bar touching both stop and target is conservatively counted as a stop.
    for k in range(entry_i,entry_i+288):
        if rows[k][11]<=stop:return 'stop'
        if rows[k][10]>=target:return 'target_2R'
    return 'neither'


def read_db(cn,symbol):
    sql='''SELECT r.t,r.end_t,r.future_open,r.future_close,r.spot_taker_delta_quote_usdt,
            r.futures_taker_delta_native,s.v_quote_usdt,f.v_native,
            r.binance_oi_native,r.binance_oi_observed_t,f.h,f.l
            FROM research_5m r JOIN spot_5m s ON s.symbol=r.symbol AND s.t=r.t
             JOIN futures_5m f ON f.symbol=r.symbol AND f.t=r.t WHERE r.symbol=? ORDER BY r.t'''
    return cn.execute(sql,(symbol,)).fetchall()


def validate_history(rows):
    if len(rows)<500 or any(not math.isfinite(r[2]) or r[2]<=0 or not math.isfinite(r[3]) or r[3]<=0 for r in rows):
        raise ValueError('invalid price history')
    for a,b in zip(rows,rows[1:]):
        if b[0]-a[0]!=300 or a[1]!=a[0]+300 or b[1]!=b[0]+300:
            raise ValueError('gap or invalid 5m timestamp; abort rather than joining')


def run(db,output,provenance):
    output.mkdir(parents=True,exist_ok=True)
    cn=sqlite3.connect('file:'+str(db.resolve())+'?mode=ro',uri=True)
    cn.row_factory=None
    if cn.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('database integrity failed')
    columns={r[1] for r in cn.execute('pragma table_info(research_5m)')}
    if not REQUIRED.issubset(columns):raise ValueError('wrong V2 schema')
    digest=hashlib.sha256()
    with db.open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):
            digest.update(block)
    report={'model_version':MODEL_VERSION,'source_warehouse_run':provenance,
            'source_sqlite_sha256':digest.hexdigest(),
            'research_status':'EXPLORATORY_REUSED_24M_DATA_NOT_FRESH_OUT_OF_SAMPLE',
            'explicit_purpose':'Find better BTC/ETH BUY timing, not just turning-point labels',
            'capital_deployment_authorized':False,'live_buy_signal_available':False,
            'signal_policy':'All BUY_SETUP_* labels are historical research classifications; live order/alert gate remains DISABLED.',
            'rules':{'shock_4h_drop':SHOCK,'event_cooldown_hours':24,'decision_delay_hours':2,
                     'entry':'second following 5m candle open: 5m after full decision candle close',
                     'strict':'price reclaimed shock close + positive 1h/last15m spot taker imbalance + 2pp 1h improvement + 1h OI change >= -0.1% + <=3% structural risk',
                     'early':'price reclaimed shock close + positive 1h and last15m spot imbalance + <=3% structural risk',
                     'price_invalidation':'lowest futures low between shock and decision minus 0.1% buffer',
                     'risk_measurement':'risk calculated at decision close, no advance knowledge of actual entry open',
                     'cost_roundtrip_pct':0.14,'cost_sensitivity_pct':[.14,.22,.35],
                     'period_horizons_hours':[1,4,24]},
            'caveats':['Not live executable: historical Binance OI publication delay is an assumed +5m, not independently verified',
                       'Existing 24-month dataset used in previous development; all chronological holdout results are contaminated by prior viewing',
                       'Backtest does not imply validated edge or recommend deploying capital',
                       'No complete historical forced liquidation tape or order-book replay; no claim to prove absorption',
                       '5m taker volume is an aggregate delta proxy, not tick-by-tick CVD',
                       'No funding P&L, leverage/margin/liquidation risk or variable execution costs in returns',
                       'Same 24h event cooldown makes 24h outcome windows essentially nonoverlapping, but event selection is nonrandom',
                       'No purchase order is placed; future forward-paper evidence must be collected'],
            'by_symbol':{}}
    full=[]
    for sym in ('BTC','ETH'):
        rows=read_db(cn,sym);validate_history(rows)
        p={'sv':prefix(rows,6),'sd':prefix(rows,4),'fv':prefix(rows,7),'fd':prefix(rows,5)}
        cutoff=rows[0][0] + int(((rows[-1][0]-rows[0][0])*.7)//300)*300
        eligible=0;purge=0;unavailable=0;cooldown_until=-1
        records=[];potential=[]
        for i in range(60,len(rows)):
            if rows[i][0]<cooldown_until:continue
            if (rows[i][3]/rows[i-48][3]-1)>SHOCK[sym]:continue
            # Anchor independent of future observations and of feature outputs.
            cooldown_until=rows[i][0]+COOLDOWN
            eligible+=1
            j=i+WAIT
            if j>=len(rows):
                continue
            feature=calc_features(rows,i,j,p)
            state=classify(feature)
            dec={
                'symbol':sym,'model_version':MODEL_VERSION,'shock_utc':iso(rows[i][1]),
                'decision_utc':iso(rows[j][1]),'state':state,
                'shock_4h_change_pct':round(100*(rows[i][3]/rows[i-48][3]-1),5),
                'decision_close':round(feature['decision_close'],6),
                'invalidation_price':round(feature['invalidation_price'],6),
                'decision_invalidation_risk_pct':round(feature['risk_to_invalidation_pct_at_decision'],4),
                'spot_1h_imbalance_pct':round(100*feature['spot_1h_imbalance'],4) if feature['spot_1h_imbalance'] is not None else None,
                'spot_last15_imbalance_pct':round(100*feature['spot_last15_imbalance'],4) if feature['spot_last15_imbalance'] is not None else None,
                'spot_improvement_pp':round(feature['spot_improvement_pp'],4) if feature['spot_improvement_pp'] is not None else None,
                'futures_1h_imbalance_pct':round(100*feature['futures_1h_imbalance'],4) if feature['futures_1h_imbalance'] is not None else None,
                'oi_1h_change_pct':round(feature['oi_1h_change_pct'],5) if feature['oi_valid'] else None,
                'oi_4h_change_pct':round(feature['oi_4h_change_pct'],5) if feature['oi_valid'] else None,
                'oi_verification': 'audited_with_5m_assumed_release_guard' if feature['oi_valid'] else 'missing_or_unsafe',
                'price_reclaim':int(feature['price_reclaim']),
                'entry_utc':None,'entry_open':None,'phase': 'development' if rows[i][0]<cutoff else 'historical_last30pct_already_examined',
                'outcome_available':False,
                'net_1h':None,'net_4h':None,'net_24h':None,'mae_24h':None,'mfe_24h':None,'stop_hit_24h':None,'first_hit_24h':None}
            potential.append(dec.copy())
            entry_i=j+ENTRY_WAIT
            if entry_i+288>=len(rows):unavailable+=1;continue
            if rows[i][0]<cutoff<=rows[entry_i+288][0]:purge+=1;continue
            ent=rows[entry_i][2]
            dec['entry_utc']=iso(rows[entry_i][0]);dec['entry_open']=round(ent,6)
            dec['outcome_available']=True
            for name,n in HORIZONS.items():
                future=rows[entry_i+n][2]
                dec['net_'+name]=round((future/ent-1)*100-.14,5)
                if name=='24h':
                    dec['mae_24h']=round(100*(min(rows[k][11] for k in range(entry_i,entry_i+n))/ent-1),5)
                    dec['mfe_24h']=round(100*(max(rows[k][10] for k in range(entry_i,entry_i+n))/ent-1),5)
            stop=feature['invalidation_price']
            dec['stop_hit_24h']=int(any(rows[k][11]<=stop for k in range(entry_i,entry_i+288)))
            if stop<ent:
                target=ent+2*(ent-stop)
                dec['first_hit_24h']=first_hit(rows,entry_i,stop,target)
            else:dec['first_hit_24h']='invalid_entry_below_stop'
            records.append(dec);full.append(dec)
        by_state={}
        for label in ('BUY_SETUP_STRICT','BUY_SETUP_EARLY','WATCH_NO_BUY','SELLING_NO_BUY','NO_DATA'):
            chunk=[x for x in records if x['state']==label]
            by_state[label]={'overall':statistics_for(chunk),
                             'development':statistics_for([x for x in chunk if x['phase']=='development']),
                             'historical_last30pct_already_examined':statistics_for([x for x in chunk if x['phase']!='development'])}
        report['by_symbol'][sym]={'history_utc':[iso(rows[0][0]),iso(rows[-1][1])],
          'cutoff_utc':iso(cutoff),'shock_anchors_total':eligible,'anchors_with_2h_decision':len(potential),
          'evaluated':len(records),'purged_boundary_events':purge,'unavailable_future_outcome':unavailable,
          'states':by_state,'all_events':{'overall':statistics_for(records)},
          'latest_observed_market_utc':iso(rows[-1][1]),
          'historical_signal_latest':potential[-1] if potential else None,
          'live_buy_signal_available':False,
          'live_signal_reason':'Batch warehouse last available data; not a continuous tick-level order/entry feed'}
    cn.close()
    path=output/'v3_buy_timing_events.csv'
    fields=list(full[0]) if full else []
    with path.open('w',newline='',encoding='utf8') as f:
        if fields:
            writer=csv.DictWriter(f,fieldnames=fields)
            writer.writeheader();writer.writerows(full)
    (output/'v3_buy_timing_report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf8')
    print('V3 RESULTS '+json.dumps({s:{'anchors':d['shock_anchors_total'],'evaluated':d['evaluated'],
         'strict':d['states']['BUY_SETUP_STRICT']['overall'],
         'early':d['states']['BUY_SETUP_EARLY']['overall']} for s,d in report['by_symbol'].items()},ensure_ascii=False))
    return report


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--db',required=True,type=Path)
    p.add_argument('--out',required=True,type=Path)
    p.add_argument('--warehouse-run',default='unknown')
    args=p.parse_args()
    if not args.db.is_file():raise SystemExit('No warehouse: '+str(args.db))
    run(args.db,args.out,args.warehouse_run)

if __name__=='__main__':main()
