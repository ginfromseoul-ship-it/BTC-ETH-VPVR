import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from v4_ops_audit import (audit, cadence_summary, flatten_run_pages, individual_health,
                          append_unique, SOURCE_NAMES, iso)

NOW=datetime(2026,10,26,4,0,tzinfo=timezone.utc)


def runs(name,interval_min=15,days=2, event='schedule'):
    out=[]
    k=0
    at=NOW-timedelta(days=days)
    while at<NOW:
        k+=1
        out.append({'id':k,'name':name,'created_at':iso(at),
                    'event':event,'status':'completed','conclusion':'success'})
        at+=timedelta(minutes=interval_min)
    return {'workflow_runs':out}


def latest(age=7, skew=0, state='MIXED_WAIT'):
    s=iso(NOW-timedelta(minutes=age))
    f=iso(NOW-timedelta(minutes=age+skew))
    return {'version':'v4_prospective_paper_2026-10-26_01','generated_utc':iso(NOW),
            'new_snapshots_saved':2,'new_paper_entries':0,'new_forward_outcomes':0,
            'input_sources':{'spot_collected_utc':s,'futures_collected_utc':f},
            'states': {sym:{'state':state,'missing':[],
                            'spot_bar_end_utc':s,'futures_bar_end_utc':f}
                       for sym in ('BTC','ETH')}}


class AuditTests(unittest.TestCase):
    def test_healthy_recent_sources(self):
        a=individual_health(latest(),NOW,'BTC')
        self.assertEqual(a['status'],'FRESH_AND_USABLE')

    def test_source_stale_blocks_healthy(self):
        self.assertIn('spot_candle_fresh',individual_health(latest(age=55),NOW,'BTC')['failed_checks'])

    def test_skew_blocks(self):
        self.assertIn('cross_source_bar_skew_ok',individual_health(latest(skew=20),NOW,'ETH')['failed_checks'])

    def test_invalid_decision_blocks(self):
        self.assertIn('usable_model_decision',individual_health(latest(state='DATA_UNAVAILABLE'),NOW,'BTC')['failed_checks'])

    def test_full_cron_run_coverage(self):
        a=cadence_summary(runs(SOURCE_NAMES['spot']),NOW,SOURCE_NAMES['spot'])
        self.assertEqual(a['status'],'OK')
        self.assertEqual(a['observed_scheduled_starts'],96)
        self.assertEqual(a['approximate_start_coverage_pct'],100)

    def test_half_cron_run_coverage_warns(self):
        a=cadence_summary(runs(SOURCE_NAMES['spot'],interval_min=30),NOW,SOURCE_NAMES['spot'])
        self.assertEqual(a['status'],'LOW_SCHEDULE_START_COVERAGE')
        self.assertEqual(a['approximate_start_coverage_pct'],50)

    def test_manual_trigger_not_counted_as_scheduled(self):
        a=cadence_summary(runs(SOURCE_NAMES['spot'],event='workflow_dispatch'),NOW,SOURCE_NAMES['spot'])
        self.assertEqual(a['observed_scheduled_starts'],0)

    def test_pagination_deduplicates_run_ids(self):
        x={'workflow_runs':[{'id':1},{'id':2}]}
        self.assertEqual(len(flatten_run_pages([x,x])),2)

    def test_oldest_page_missing_not_misreported_as_missed(self):
        x=runs(SOURCE_NAMES['spot'])
        x['workflow_runs']=x['workflow_runs'][-10:]
        a=cadence_summary(x,NOW,SOURCE_NAMES['spot'])
        self.assertEqual(a['status'],'INSUFFICIENT_API_HISTORY')
        self.assertIsNone(a['approximate_start_coverage_pct'])

    def test_api_failure_explicitly_unavailable(self):
        self.assertEqual(cadence_summary(None,NOW,'spot')['status'],'API_UNAVAILABLE')

    def test_triggered_rerun_append_only(self):
        with tempfile.TemporaryDirectory() as td:
            spot=runs(SOURCE_NAMES['spot'])
            deriv=runs(SOURCE_NAMES['derivatives'])
            r=audit(latest(),spot,deriv,NOW,td,'123','workflow_run',SOURCE_NAMES['spot'])
            self.assertEqual(r['status'],'OPERATING_NORMALLY')
            audit(latest(),spot,deriv,NOW,td,'123','workflow_run',SOURCE_NAMES['spot'])
            self.assertEqual(len((Path(td)/'run_audits'/'2026-10-26.jsonl').read_text().splitlines()),1)

    def test_no_entries_not_fault(self):
        with tempfile.TemporaryDirectory() as td:
            r=audit(latest(),runs(SOURCE_NAMES['spot']),runs(SOURCE_NAMES['derivatives']),NOW,td,'456')
            self.assertEqual(r['new_paper_entries'],0)
            self.assertEqual(r['status'],'OPERATING_NORMALLY')

    def test_degraded_schedule_does_not_falsify_feed(self):
        with tempfile.TemporaryDirectory() as td:
            r=audit(latest(),runs(SOURCE_NAMES['spot'],30),runs(SOURCE_NAMES['derivatives'],30),NOW,td,'456')
            self.assertEqual(r['status'],'DEGRADED_SCHEDULE')
            self.assertTrue(all(a['status']=='FRESH_AND_USABLE' for a in r['assets'].values()))

    def test_missing_api_does_not_claim_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            r=audit(latest(),None,None,NOW,td,'456')
            self.assertEqual(r['status'],'SOURCE_CADENCE_UNVERIFIED')

    def test_future_dated_feed_blocked(self):
        self.assertIn('spot_candle_fresh',individual_health(latest(age=-20),NOW,'BTC')['failed_checks'])

if __name__=='__main__':unittest.main()
