import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from v4_6_gap_guard import (VERSION,MAX_AGE_MIN,parse_time,iso,source_state,evaluate_dispatch,
                             recent_gap_metrics,run, load, GithubAPI)

T=datetime(2026,10,26,7,0,tzinfo=timezone.utc)


def sample(kind,age=10):
    t=iso(T-timedelta(minutes=age))
    if kind=='spot':
        return {'collected_utc':t,'assets':{s:{'quality':{'spot_flow':'ok'},'spot':{'flow':{'last_closed_bar_utc':t}}} for s in ('BTCUSDT','ETHUSDT')}}
    return {'generated_utc':t,'assets':{s:{'quality':{k:'ok' for k in ('open_interest','futures_flow','liquidations','funding')},'futures_flow':{'last_complete_utc':t}} for s in ('BTC','ETH')}}


def entry(age=10,event='schedule',status='completed',conclusion='success'):
    return {'created_at':iso(T-timedelta(minutes=age)), 'event':event,'status':status,'conclusion':conclusion}


class FakeAPI:
    def __init__(self,runs):self.history=runs;self.dispatched=[]
    def runs(self,workflow):return self.history[workflow]
    def dispatch(self,workflow):self.dispatched.append(workflow)


def setup_root(folder,spot,deriv):
    p=Path(folder)/'data'
    p.mkdir(exist_ok=True)
    for name,payload in [('market_flow_latest.json',spot),('coinalyze_derivatives_latest.json',deriv)]:
        if payload is not None:(p/name).write_text(json.dumps(payload))


def api(recent_spot=None,recent_deriv=None):
    return FakeAPI({'market_flow.yml':recent_spot if recent_spot is not None else [entry(30)],
                    'coinalyze.yml':recent_deriv if recent_deriv is not None else [entry(30)]})


class TimeAndQuality(unittest.TestCase):
    def test_timestamps_timezone_required(self):
        with self.assertRaises(ValueError):parse_time('2026-10-26T06:30:00')

    def test_iso_roundtrip(self):
        self.assertEqual(parse_time(iso(T)),T)

    def test_missing_invalid(self):
        self.assertEqual(source_state('spot',None,T)['state'],'INVALID')
        self.assertEqual(source_state('spot',{},T)['state'],'INVALID')

    def test_both_are_fresh(self):
        self.assertEqual(source_state('spot',sample('spot'),T)['state'],'FRESH')
        self.assertEqual(source_state('derivatives',sample('derivatives'),T)['state'],'FRESH')

    def test_boundary_fresh(self):
        self.assertEqual(source_state('spot',sample('spot',MAX_AGE_MIN),T)['state'],'FRESH')

    def test_stale(self):
        self.assertEqual(source_state('spot',sample('spot',MAX_AGE_MIN+1),T)['state'],'STALE')
        self.assertEqual(source_state('derivatives',sample('derivatives',40),T)['state'],'STALE')

    def test_future_stamp_fails(self):
        self.assertEqual(source_state('spot',sample('spot',-7),T)['reason'],'future_source_timestamp')

    def test_derivatives_core_quality_all_required(self):
        s=sample('derivatives');s['assets']['ETH']['quality']['liquidations']='unavailable'
        self.assertEqual(source_state('derivatives',s,T)['state'],'QUALITY_FAIL')

    def test_spot_core_quality_required(self):
        s=sample('spot');s['assets']['BTCUSDT']['quality']['spot_flow']='unavailable'
        self.assertEqual(source_state('spot',s,T)['state'],'QUALITY_FAIL')

    def test_optional_funding_missing_does_not_force_recovery(self):
        s=sample('derivatives')
        s['assets']['BTC']['quality']['funding']='unavailable'
        self.assertEqual(source_state('derivatives',s,T)['state'],'FRESH')

    def test_fresh_generation_cannot_mask_stale_candle(self):
        s=sample('derivatives',3)
        s['assets']['ETH']['futures_flow']['last_complete_utc']=iso(T-timedelta(minutes=55))
        x=source_state('derivatives',s,T)
        self.assertEqual(x['state'],'STALE_BAR')
        self.assertEqual(x['bar_age_minutes'],55)

    def test_missing_bar_time_fails_closed(self):
        s=sample('spot',8)
        del s['assets']['BTCUSDT']['spot']['flow']['last_closed_bar_utc']
        self.assertEqual(source_state('spot',s,T)['state'],'INVALID')


class DispatchPlan(unittest.TestCase):
    def stale(self):return {'state':'STALE'}
    def test_fresh_never_dispatches_even_missing_run_history(self):
        self.assertEqual(evaluate_dispatch({'state':'FRESH'},None,T),'NO_ACTION_FRESH')

    def test_missing_history_fails_closed(self):
        self.assertEqual(evaluate_dispatch(self.stale(),None,T),'BLOCKED_RUN_HISTORY_UNAVAILABLE')

    def test_invalid_run_history_fails_closed(self):
        self.assertEqual(evaluate_dispatch(self.stale(),[{'created_at':'wrong'}],T),'BLOCKED_INVALID_RUN_HISTORY')

    def test_future_run_history_fails_closed(self):
        self.assertEqual(evaluate_dispatch(self.stale(),[entry(-5)],T),'BLOCKED_FUTURE_RUN_HISTORY')

    def test_pending_schedule_does_not_dispatch(self):
        self.assertEqual(evaluate_dispatch(self.stale(),[entry(2,status='in_progress',conclusion=None)],T),'WAIT_ACTIVE_SOURCE_RUN')

    def test_recent_success_grace(self):
        self.assertEqual(evaluate_dispatch(self.stale(),[entry(3)],T),'WAIT_RECENT_SUCCESS_TO_PERSIST')

    def test_recent_manual_cooldown(self):
        self.assertEqual(evaluate_dispatch(self.stale(),[entry(12,event='workflow_dispatch')],T),'WAIT_MANUAL_COOLDOWN')

    def test_manual_dispatch_hourly_limit(self):
        runs=[entry(26,event='workflow_dispatch'),entry(58,event='workflow_dispatch')]
        self.assertEqual(evaluate_dispatch(self.stale(),runs,T),'BLOCKED_HOURLY_DISPATCH_LIMIT')

    def test_normal_stale_can_dispatch(self):
        self.assertEqual(evaluate_dispatch(self.stale(),[entry(30)],T),'DISPATCH')

    def test_one_older_manual_allows_rescue(self):
        self.assertEqual(evaluate_dispatch(self.stale(),[entry(35,event='workflow_dispatch')],T),'DISPATCH')

    def test_old_queued_run_does_not_overtrigger(self):
        self.assertEqual(evaluate_dispatch(self.stale(),[entry(36,status='queued')],T),'WAIT_ACTIVE_SOURCE_RUN')


class Integration(unittest.TestCase):
    def test_one_stale_source_dispatches_only_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup_root(tmp,sample('spot'),sample('derivatives',40))
            fake=api()
            d=run(T,tmp,fake,False)
            self.assertEqual(d['plans']['spot'],'NO_ACTION_FRESH')
            self.assertEqual(d['actions']['derivatives'],'DISPATCHED')
            self.assertEqual(fake.dispatched,['coinalyze.yml'])
            self.assertFalse(d['orders_enabled'])
            self.assertFalse(d['synthetic_snapshots_created'])

    def test_cross_feed_bar_skew_causes_rescue(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup_root(tmp,sample('spot',1),sample('derivatives',17))
            fake=api()
            r=run(T,tmp,fake,False)
            self.assertEqual(r['sources']['derivatives']['state'],'SKEWED')
            self.assertEqual(r['sources']['derivatives']['cross_source_skew_minutes'],16)
            self.assertEqual(fake.dispatched,['coinalyze.yml'])

    def test_normal_five_minute_bar_skew_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup_root(tmp,sample('spot',3),sample('derivatives',8))
            fake=api()
            r=run(T,tmp,fake,False)
            self.assertEqual(r['plans']['spot'],'NO_ACTION_FRESH')
            self.assertEqual(r['plans']['derivatives'],'NO_ACTION_FRESH')
            self.assertEqual(fake.dispatched,[])

    def test_dry_run_no_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup_root(tmp,sample('spot',50),sample('derivatives',50))
            fake=api()
            r=run(T,tmp,fake,True)
            self.assertEqual(r['actions']['spot'],'WOULD_DISPATCH_DRY_RUN')
            self.assertEqual(r['actions']['derivatives'],'WOULD_DISPATCH_DRY_RUN')
            self.assertEqual(fake.dispatched,[])

    def test_both_stale_action_capped_by_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup_root(tmp,sample('spot',40),sample('derivatives',40))
            fake=api(recent_spot=[entry(9,event='workflow_dispatch')],recent_deriv=[entry(42)])
            r=run(T,tmp,fake,False)
            self.assertEqual(r['actions']['spot'],'WAIT_MANUAL_COOLDOWN')
            self.assertEqual(r['actions']['derivatives'],'DISPATCHED')
            self.assertEqual(fake.dispatched,['coinalyze.yml'])

    def test_missing_file_not_replaced_with_fake(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup_root(tmp,None,sample('derivatives'))
            fake=api()
            d=run(T,tmp,fake,False)
            self.assertEqual(d['sources']['spot']['state'],'INVALID')
            self.assertIn('market_flow.yml',fake.dispatched)
            self.assertFalse((Path(tmp)/'data/market_flow_latest.json').exists())

    def test_deterministic_same_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup_root(tmp,sample('spot',40),sample('derivatives',25))
            self.assertEqual(run(T,tmp,api(),True),run(T,tmp,api(),True))

    def test_bad_api_history_prevents_any_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup_root(tmp,sample('spot',40),sample('derivatives',40))
            fake=FakeAPI({'market_flow.yml':[entry(40)],'coinalyze.yml':None})
            report=run(T,tmp,fake,False)
            self.assertEqual(report['actions']['derivatives'],'BLOCKED_RUN_HISTORY_UNAVAILABLE')
            # A separate working history for spot is actionable. GitHub API errors themselves throw.
            self.assertEqual(report['actions']['spot'],'DISPATCHED')


class Coverage(unittest.TestCase):
    def test_real_gap_118_min_reported_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'snapshots'/T.date().isoformat().__str__()
            target=target.with_suffix('.jsonl')
            target.parent.mkdir(parents=True,exist_ok=True)
            examples=[(-118,'NO_DOWNTURN_SETUP',[]),(-62,'DATA_UNAVAILABLE',['stale_or_future_futures']),
                      (0,'NO_DOWNTURN_SETUP',[])]
            with target.open('w') as f:
                for minutes,state,missing in examples:
                    f.write(json.dumps({'symbol':'BTC','first_seen_utc':iso(T+timedelta(minutes=minutes)),
                                        'state':state,'missing':missing})+'\n')
            result=recent_gap_metrics(tmp,T)['BTC']
            self.assertEqual(result['observations'],3)
            self.assertEqual(result['full_feature_verified'],2)
            self.assertEqual(result['source_unverified'],1)
            self.assertEqual(result['largest_verified_pair_gap_min'],118)
            self.assertEqual(result['verified_pair_gaps_over_90min'],1)

    def test_empty_coverage_null_not_zero_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            x=recent_gap_metrics(tmp,T)['ETH']
            self.assertIsNone(x['largest_verified_pair_gap_min'])
            self.assertIsNone(x['last_verified_age_min'])
            self.assertEqual(x['observations'],0)


if __name__=='__main__':
    unittest.main()
