import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from v4_3_cohort_compare import (evaluate,iso,jsonl,parse_time,VERSION,
    observed_snapshot,day_path,snapshots_from_v4,ARMS)

T=datetime(2026,10,26,4,0,tzinfo=timezone.utc)


def snap(t,symbol='BTC',state='MIXED_WAIT',drop=-1.5,price=100,seen_delay=0,usable=True):
    seen=t+timedelta(minutes=seen_delay)
    return {'snapshot_id':symbol+'_'+iso(t), 'symbol':symbol,'version':'v4.1',
            'first_seen_utc':iso(seen),'spot_bar_end_utc':iso(t),
            'reference_close_usdt':price,'spot_price_observation_usable':usable,
            'state':state,'evidence':{'spot_price_change_4h_pct':drop},'missing':[]}


def put(root,x):
    path=Path(root)/'snapshots'/parse_time(x['first_seen_utc']).date().isoformat()
    path=path.with_suffix('.jsonl')
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a',encoding='utf8') as f:f.write(json.dumps(x)+'\n')


def records(root,kind,t=T):
    path=Path(root)/'comparison_v4_3'/kind/(t.date().isoformat()+'.jsonl')
    return jsonl(path)


class ProtocolTests(unittest.TestCase):
    def test_bootstrap_never_imports_old_observation_as_paper_cohort(self):
        with tempfile.TemporaryDirectory() as d:
            put(d,snap(T-timedelta(minutes=10)))
            a=evaluate(T,Path(d))
            self.assertEqual(a['status'],'BOOTSTRAPPED_NO_HISTORICAL_BACKFILL')
            self.assertFalse(records(d,'cohorts'))
            b=evaluate(T+timedelta(minutes=1),Path(d))
            self.assertEqual(b['by_symbol']['BTC']['cohorts_started'],0)

    def test_first_new_shock_starts_single_cohort_and_no_same_snapshot_entry(self):
        with tempfile.TemporaryDirectory() as d:
            evaluate(T-timedelta(minutes=1),Path(d))
            put(d,snap(T))
            a=evaluate(T,Path(d))
            self.assertEqual(a['new_cohorts'],1)
            self.assertEqual(a['new_arm_dispositions'],0)
            self.assertEqual(a['by_symbol']['BTC']['cohorts_started'],1)
            self.assertFalse(records(d,'entries'))

    def test_no_cohort_for_stale_or_false_drop(self):
        with tempfile.TemporaryDirectory() as d:
            evaluate(T-timedelta(hours=1),Path(d))
            put(d,snap(T,symbol='BTC',drop=-.8))
            put(d,snap(T,symbol='ETH',drop=-2,usable=False))
            self.assertEqual(evaluate(T,Path(d))['new_cohorts'],0)

    def test_signal_and_three_entries_require_later_separate_seen_price(self):
        with tempfile.TemporaryDirectory() as d:
            evaluate(T-timedelta(minutes=1),Path(d))
            observations=[snap(T),snap(T+timedelta(minutes=15),state='REVERSAL_WATCH',price=99),
                snap(T+timedelta(minutes=30),state='MIXED_WAIT',price=100),
                snap(T+timedelta(minutes=45),state='PAPER_CANDIDATE_UNVALIDATED',price=100.5),
                snap(T+timedelta(minutes=60),state='NO_DOWNTURN_SETUP',price=101)]
            for x in observations:
                put(d,x)
                a=evaluate(parse_time(x['first_seen_utc']),Path(d))
            es=records(d,'entries')
            self.assertEqual(len(es),3)
            by={e['arm']:e for e in es}
            self.assertEqual(set(by),set(ARMS))
            self.assertTrue(all(x['status']=='ENTRY_REFERENCE_OBSERVED' for x in es))
            self.assertEqual(by['DIP_NEXT']['entry_first_seen_utc'],iso(T+timedelta(minutes=15)))
            self.assertEqual(by['WATCH_NEXT']['entry_first_seen_utc'],iso(T+timedelta(minutes=30)))
            self.assertEqual(by['CONFIRM_NEXT']['entry_first_seen_utc'],iso(T+timedelta(minutes=60)))
            self.assertEqual(a['by_symbol']['BTC']['by_arm']['CONFIRM_NEXT']['entered'],1)

    def test_recent_but_pre_detection_candle_cannot_be_a_fill(self):
        with tempfile.TemporaryDirectory() as d:
            evaluate(T-timedelta(minutes=1),Path(d))
            put(d,snap(T,state='PAPER_CANDIDATE_UNVALIDATED',seen_delay=20))
            evaluate(T+timedelta(minutes=20),Path(d))
            # New snapshot first seen later; source candle closed before signal detection.
            put(d,snap(T+timedelta(minutes=15),state='MIXED_WAIT',price=99,seen_delay=10))
            a=evaluate(T+timedelta(minutes=25),Path(d))
            self.assertEqual(a['new_arm_dispositions'],0)
            put(d,snap(T+timedelta(minutes=30),state='MIXED_WAIT',price=98))
            evaluate(T+timedelta(minutes=30),Path(d))
            es=records(d,'entries')
            self.assertEqual(len(es),3)
            self.assertTrue(all(e['entry_spot_bar_end_utc']==iso(T+timedelta(minutes=30)) for e in es))

    def test_no_next_price_is_terminal_missing_not_imputed(self):
        with tempfile.TemporaryDirectory() as d:
            evaluate(T-timedelta(minutes=1),Path(d))
            put(d,snap(T,state='PAPER_CANDIDATE_UNVALIDATED'))
            evaluate(T,Path(d))
            a=evaluate(T+timedelta(minutes=46),Path(d))
            self.assertEqual(a['new_arm_dispositions'],3)
            self.assertTrue(all(x['status']=='NO_TIMELY_POST_SIGNAL_PRICE' for x in records(d,'entries')))
            put(d,snap(T+timedelta(minutes=60),price=100))
            a=evaluate(T+timedelta(minutes=60),Path(d))
            self.assertEqual(a['new_arm_dispositions'],0)

    def test_skipped_confirmation_stays_explicit_after_four_hours(self):
        with tempfile.TemporaryDirectory() as d:
            evaluate(T-timedelta(minutes=1),Path(d))
            put(d,snap(T))
            evaluate(T,Path(d))
            evaluate(T+timedelta(hours=4,minutes=30),Path(d))
            self.assertEqual(len(records(d,'entries')),1) # baseline expired at 45min
            a=evaluate(T+timedelta(hours=4,minutes=36),Path(d))
            self.assertEqual(a['new_arm_dispositions'],2)
            statuses={x['arm']:x['status'] for x in records(d,'entries')}
            self.assertEqual(statuses['DIP_NEXT'],'NO_TIMELY_POST_SIGNAL_PRICE')
            self.assertEqual(statuses['WATCH_NEXT'],'NO_SIGNAL_WITHIN_4H')
            self.assertEqual(statuses['CONFIRM_NEXT'],'NO_SIGNAL_WITHIN_4H')

    def test_paper_returns_have_horizon_after_entry_first_seen(self):
        with tempfile.TemporaryDirectory() as d:
            evaluate(T-timedelta(minutes=1),Path(d))
            put(d,snap(T,state='PAPER_CANDIDATE_UNVALIDATED'))
            evaluate(T,Path(d))
            put(d,snap(T+timedelta(minutes=15),price=101))
            evaluate(T+timedelta(minutes=15),Path(d))
            self.assertEqual(len(records(d,'entries')),3)
            put(d,snap(T+timedelta(minutes=75),price=104,drop=0))
            r=evaluate(T+timedelta(minutes=75),Path(d))
            outs=records(d,'outcomes')
            self.assertEqual(len(outs),3)
            self.assertEqual(r['new_outcomes'],3)
            self.assertTrue(all(x['hours']==1 and x['status']=='OUTCOME_REFERENCE_OBSERVED' for x in outs))
            self.assertAlmostEqual(outs[0]['net_pct_by_cost']['0.14'],round((104/101-1)*100-.14,5))
            self.assertEqual(evaluate(T+timedelta(minutes=76),Path(d))['new_outcomes'],0)

    def test_missing_future_price_is_not_zero_or_backfilled_late(self):
        with tempfile.TemporaryDirectory() as d:
            evaluate(T-timedelta(minutes=1),Path(d))
            put(d,snap(T,state='PAPER_CANDIDATE_UNVALIDATED'))
            evaluate(T,Path(d))
            put(d,snap(T+timedelta(minutes=15),price=101));evaluate(T+timedelta(minutes=15),Path(d))
            evaluate(T+timedelta(hours=2,minutes=10),Path(d))
            outs=records(d,'outcomes')
            self.assertEqual(len(outs),3)
            self.assertTrue(all(x['status']=='UNAVAILABLE_NO_TIMELY_FORWARD_PRICE' for x in outs))
            self.assertTrue(all(x['gross_pct'] is None for x in outs))
            put(d,snap(T+timedelta(hours=2,minutes=15),price=104,drop=0))
            self.assertEqual(evaluate(T+timedelta(hours=2,minutes=15),Path(d))['new_outcomes'],0)

    def test_one_episode_per_symbol_per_24h(self):
        with tempfile.TemporaryDirectory() as d:
            evaluate(T-timedelta(minutes=1),Path(d))
            for dt in (0,2,23,24,25):
                at=T+timedelta(hours=dt)
                put(d,snap(at,price=100-dt))
                evaluate(at,Path(d))
            cohorts=records(d,'cohorts',T)+records(d,'cohorts',T+timedelta(days=1))
            self.assertEqual(len(cohorts),2)

    def test_episode_not_doubled_by_rerun_and_source_id(self):
        with tempfile.TemporaryDirectory() as d:
            evaluate(T-timedelta(minutes=1),Path(d))
            a=snap(T);put(d,a);put(d,a)
            first=evaluate(T,Path(d))
            second=evaluate(T,Path(d))
            self.assertEqual(first['new_cohorts'],1)
            self.assertEqual(second['new_cohorts'],0)
            self.assertEqual(len(records(d,'cohorts')),1)

    def test_future_timestamped_snapshot_cannot_trigger_earlier_cohort(self):
        with tempfile.TemporaryDirectory() as d:
            evaluate(T-timedelta(minutes=1),Path(d))
            put(d,snap(T+timedelta(minutes=10)))
            a=evaluate(T,Path(d))
            self.assertEqual(a['new_cohorts'],0)

    def test_close_older_than_35min_cannot_start_or_mark(self):
        x=snap(T,seen_delay=36)
        self.assertFalse(observed_snapshot(x,T+timedelta(minutes=36)))

    def test_no_24h_results_after_only_one_hour(self):
        with tempfile.TemporaryDirectory() as d:
            evaluate(T-timedelta(minutes=1),Path(d))
            put(d,snap(T,state='PAPER_CANDIDATE_UNVALIDATED'));evaluate(T,Path(d))
            put(d,snap(T+timedelta(minutes=15),price=101));evaluate(T+timedelta(minutes=15),Path(d))
            put(d,snap(T+timedelta(minutes=75),price=102,drop=0));a=evaluate(T+timedelta(minutes=75),Path(d))
            self.assertEqual(a['by_symbol']['BTC']['by_arm']['DIP_NEXT']['24h_outcomes'],0)
            self.assertIsNone(a['by_symbol']['BTC']['paired_diagnostics']['CONFIRM_NEXT']['mean_net_24h_difference_vs_dip_pp'])

    def test_no_signal_includes_missed_upside_diagnostic(self):
        with tempfile.TemporaryDirectory() as d:
            evaluate(T-timedelta(minutes=1),Path(d))
            put(d,snap(T));evaluate(T,Path(d))
            put(d,snap(T+timedelta(minutes=15),state='MIXED_WAIT',price=101,drop=0))
            evaluate(T+timedelta(minutes=15),Path(d))
            evaluate(T+timedelta(hours=4,minutes=36),Path(d))
            put(d,snap(T+timedelta(hours=24,minutes=15),state='NO_DOWNTURN_SETUP',price=111,drop=0))
            out=evaluate(T+timedelta(hours=24,minutes=15),Path(d))
            btc=out['by_symbol']['BTC']
            self.assertEqual(btc['by_arm']['DIP_NEXT']['24h_outcomes'],1)
            self.assertEqual(btc['by_arm']['CONFIRM_NEXT']['skipped_no_signal'],1)
            missed=btc['paired_diagnostics']['CONFIRM_NEXT']
            self.assertEqual(missed['skipped_confirmations_with_baseline_24h'],1)
            self.assertAlmostEqual(missed['baseline_24h_mean_net_on_no_signal_cohorts'],round((111/101-1)*100-.14,5))
            self.assertEqual(missed['matched_cohorts'],0)

    def test_protocol_mismatch_halts(self):
        with tempfile.TemporaryDirectory() as d:
            evaluate(T,Path(d))
            p=Path(d)/'comparison_v4_3/protocol.json'
            o=json.loads(p.read_text());o['historical_snapshots_excluded']=False;p.write_text(json.dumps(o))
            with self.assertRaisesRegex(ValueError,'protocol mismatch'):
                evaluate(T+timedelta(minutes=5),Path(d))

if __name__=='__main__':unittest.main()
