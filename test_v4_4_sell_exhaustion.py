import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from v4_4_sell_exhaustion import (decide,evaluate,iso,VERSION,STATE_RECOVERY,
    closest_after,paper_summary,usable_for_state,MAX_DECISION_AGE)
from v4_3_cohort_compare import jsonl,day_path

UTC=timezone.utc
START=datetime(2026,10,26,6,0,tzinfo=UTC)


def snap(t,symbol='BTC',price=100,drop=-1.8,spot1=45,recent=40,future=50,oi=-.5,
         state='MIXED_WAIT',missing=None,bar_delay=5):
    return {'snapshot_id':symbol+':'+iso(t),'symbol':symbol,'first_seen_utc':iso(t),
      'spot_bar_end_utc':iso(t-timedelta(minutes=bar_delay)),
      'futures_bar_end_utc':iso(t-timedelta(minutes=bar_delay)),
      'reference_close_usdt':price,'spot_price_observation_usable':True,
      'state':state,'missing':missing or [],'evidence':{
      'spot_buy_share_1h_pct':spot1,'spot_buy_share_last15m_pct':recent,
      'futures_buy_share_1h_pct':future,'spot_price_change_4h_pct':drop,
      'oi_native_change_1h_pct':oi,'reported_long_liquidations_1h_usd':120000,
      'reported_short_liquidations_1h_usd':2000,
      'depth_bid_ask_ratio_0_1pct_snapshot':1.1}}


def store(root,s):
    p=Path(root)/'snapshots'/(s['first_seen_utc'][:10]+'.jsonl')
    p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('a') as f:f.write(json.dumps(s)+'\n')


class MarketStateTests(unittest.TestCase):
    def test_recovery_is_proxy_paper_only(self):
        prev=snap(START,price=100)
        curr=snap(START+timedelta(minutes=20),price=100.4,spot1=53,recent=58,oi=-.5)
        d=decide(curr,[prev],START+timedelta(minutes=21))
        self.assertEqual(d['stage'],STATE_RECOVERY)
        self.assertTrue(d['entry_signal']);self.assertTrue(d['no_orders'])
        self.assertEqual(d['evidence']['previous_snapshot_id'],prev['snapshot_id'])

    def test_exhaustion_candidate_not_true_absorption(self):
        prev=snap(START,price=100,recent=39)
        now=snap(START+timedelta(minutes=20),price=99.94,spot1=48,recent=50,future=50)
        d=decide(now,[prev],START+timedelta(minutes=21))
        self.assertEqual(d['stage'],'SELL_PRESSURE_EASING_PROXY')
        self.assertFalse(d['entry_signal'])
        self.assertIn('NOT order-level absorption',d['limitations'])

    def test_futures_led_bounce_overrides_recovery(self):
        prev=snap(START,price=100)
        now=snap(START+timedelta(minutes=20),price=100.41,spot1=47,recent=60,future=61)
        d=decide(now,[prev],START+timedelta(minutes=21))
        self.assertEqual(d['stage'],'FUTURES_LED_BOUNCE_RISK')
        self.assertFalse(d['entry_signal'])

    def test_continued_selling(self):
        prev=snap(START,price=100)
        curr=snap(START+timedelta(minutes=20),price=99.5,spot1=44,recent=42)
        self.assertEqual(decide(curr,[prev],START+timedelta(minutes=21))['stage'], 'SELLING_CONTINUES_PROXY')

    def test_no_downturn_is_not_candidate(self):
        prev=snap(START,drop=+.3)
        curr=snap(START+timedelta(minutes=20),price=100.5,drop=+.5,spot1=55,recent=60)
        self.assertEqual(decide(curr,[prev],START+timedelta(minutes=21))['stage'],'NO_RECENT_DIP')

    def test_recent_dip_from_previous_is_allowed(self):
        prev=snap(START,drop=-2)
        curr=snap(START+timedelta(minutes=20),price=100.4,drop=-.7,spot1=53,recent=58)
        self.assertEqual(decide(curr,[prev],START+timedelta(minutes=21))['stage'],STATE_RECOVERY)

    def test_gap_block(self):
        prev=snap(START,price=100)
        curr=snap(START+timedelta(minutes=100),price=100.5,spot1=53,recent=58)
        self.assertEqual(decide(curr,[prev],START+timedelta(minutes=101))['stage'],'GAP_UNVERIFIED')

    def test_missing_market_data_is_not_signal(self):
        prev=snap(START)
        curr=snap(START+timedelta(minutes=20),price=100.5,spot1=53,recent=58,missing=['stale_oi'])
        self.assertFalse(usable_for_state(curr))
        self.assertEqual(decide(curr,[prev],START+timedelta(minutes=21))['stage'],'SOURCE_UNVERIFIED')

    def test_cross_source_bad_skew(self):
        curr=snap(START+timedelta(minutes=20))
        curr['futures_bar_end_utc']=iso(START-timedelta(minutes=3))
        self.assertFalse(usable_for_state(curr))

    def test_price_marks_must_follow_decision_and_close(self):
        curr=snap(START+timedelta(minutes=20),bar_delay=20)
        self.assertIsNone(closest_after([curr],'BTC',START+timedelta(minutes=15),timedelta(minutes=45)))
        later=snap(START+timedelta(minutes=25),bar_delay=5)
        self.assertIs(closest_after([curr,later],'BTC',START+timedelta(minutes=15),timedelta(minutes=45)),later)

    def test_bootstrap_does_not_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store(root,snap(START-timedelta(minutes=20)))
            first=evaluate(START,root)
            self.assertEqual(first['status'],'BOOTSTRAPPED_NO_HISTORICAL_BACKFILL')
            self.assertFalse((root/'market_state_v4_4'/'decisions').exists())

    def test_bootstrap_cannot_be_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);evaluate(START,root)
            p=root/'market_state_v4_4'/'protocol.json'
            cfg=json.loads(p.read_text());cfg['version']='MUTATED';p.write_text(json.dumps(cfg))
            with self.assertRaises(ValueError):evaluate(START+timedelta(minutes=10),root)

    def test_new_snapshot_prospectively_recorded_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            prev=snap(START-timedelta(minutes=10));store(root,prev)
            evaluate(START,root)
            curr=snap(START+timedelta(minutes=20),price=100.4,spot1=53,recent=58)
            store(root,curr)
            first=evaluate(START+timedelta(minutes=21),root)
            self.assertEqual(first['new_decisions'],1)
            self.assertEqual(first['by_symbol']['BTC']['paper_candidates'],1)
            second=evaluate(START+timedelta(minutes=23),root)
            self.assertEqual(second['new_decisions'],0)
            self.assertEqual(len(jsonl(root/'market_state_v4_4'/'decisions'/'2026-10-26.jsonl')),1)

    def test_older_than_decision_age_not_retro_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);evaluate(START,root)
            store(root,snap(START+timedelta(minutes=10)))
            r=evaluate(START+timedelta(minutes=37),root)
            self.assertEqual(r['new_decisions'],0)

    def test_signal_requires_future_observed_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            prev=snap(START-timedelta(minutes=10));store(root,prev)
            evaluate(START,root)
            curr=snap(START+timedelta(minutes=20),price=100.4,spot1=53,recent=58)
            store(root,curr)
            r=evaluate(START+timedelta(minutes=21),root)
            self.assertEqual(r['new_entries'],0)
            # Older candle closed before actual decision: cannot count as future entry.
            bad=snap(START+timedelta(minutes=30),price=100.5,bar_delay=15)
            store(root,bad);self.assertEqual(evaluate(START+timedelta(minutes=31),root)['new_entries'],0)
            good=snap(START+timedelta(minutes=45),price=100.8,bar_delay=5)
            store(root,good)
            r=evaluate(START+timedelta(minutes=46),root)
            self.assertEqual(r['new_entries'],1)
            entries=jsonl(root/'market_state_v4_4'/'entries'/'2026-10-26.jsonl')
            self.assertEqual(entries[0]['entry_reference_usdt'],100.8)
            self.assertEqual(entries[0]['entry_seen_utc'],good['first_seen_utc'])

    def test_missing_future_entry_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);prev=snap(START-timedelta(minutes=10));store(root,prev)
            evaluate(START,root)
            store(root,snap(START+timedelta(minutes=20),price=100.4,spot1=53,recent=58))
            evaluate(START+timedelta(minutes=21),root)
            r=evaluate(START+timedelta(minutes=70),root)
            self.assertEqual(r['new_entries'],1)
            es=jsonl(root/'market_state_v4_4'/'entries'/'2026-10-26.jsonl')
            self.assertEqual(es[0]['status'],'NO_TIMELY_POST_DECISION_PRICE')

    def test_outcome_future_time_and_no_double_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);prev=snap(START-timedelta(minutes=10));store(root,prev)
            evaluate(START,root)
            store(root,snap(START+timedelta(minutes=20),price=100.4,spot1=53,recent=58))
            evaluate(START+timedelta(minutes=21),root)
            store(root,snap(START+timedelta(minutes=45),price=100.8))
            evaluate(START+timedelta(minutes=46),root)
            self.assertEqual(evaluate(START+timedelta(minutes=75),root)['new_outcomes'],0)
            store(root,snap(START+timedelta(minutes=105),price=101.8,bar_delay=0))
            r=evaluate(START+timedelta(minutes=106),root)
            self.assertEqual(r['new_outcomes'],1)
            outcomes=jsonl(root/'market_state_v4_4'/'outcomes'/'2026-10-26.jsonl')
            self.assertEqual(outcomes[0]['hours'],1)
            self.assertEqual(outcomes[0]['status'],'REFERENCE_OUTCOME_OBSERVED')
            self.assertEqual(evaluate(START+timedelta(minutes=106),root)['new_outcomes'],0)

    def test_missing_horizon_not_fabricated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);prev=snap(START-timedelta(minutes=10));store(root,prev)
            evaluate(START,root)
            store(root,snap(START+timedelta(minutes=20),price=100.4,spot1=53,recent=58))
            evaluate(START+timedelta(minutes=21),root)
            store(root,snap(START+timedelta(minutes=45),price=100.8))
            evaluate(START+timedelta(minutes=46),root)
            r=evaluate(START+timedelta(minutes=151),root)
            results=jsonl(root/'market_state_v4_4'/'outcomes'/'2026-10-26.jsonl')
            self.assertEqual(results[0]['status'],'NO_TIMELY_FORWARD_PRICE')

    def test_cohort_link_uses_only_registered_earlier_cohort(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            prev=snap(START-timedelta(minutes=10));store(root,prev)
            evaluate(START,root)
            cohort= root/'comparison_v4_3'/'cohorts'/'2026-10-26.jsonl'
            cohort.parent.mkdir(parents=True)
            cohort.write_text(json.dumps({'cohort_id':'registered-1','symbol':'BTC',
                'start_seen_utc':iso(START+timedelta(minutes=5))})+'\n')
            curr=snap(START+timedelta(minutes=20),price=100.4,spot1=53,recent=58)
            store(root,curr)
            evaluate(START+timedelta(minutes=21),root)
            row=jsonl(root/'market_state_v4_4'/'decisions'/'2026-10-26.jsonl')[0]
            self.assertEqual(row['linked_v4_3_cohort_id'],'registered-1')

    def test_missing_liquidations_fails(self):
        s=snap(START)
        del s['evidence']['reported_long_liquidations_1h_usd']
        self.assertFalse(usable_for_state(s))

if __name__=='__main__':unittest.main()
