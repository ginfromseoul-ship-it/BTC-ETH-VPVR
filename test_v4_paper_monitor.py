import copy
import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from v4_paper_monitor import (feature_snapshot,pipeline,parse_jsonl,parse_time,iso,VERSION)

UTC=timezone.utc
ASOF=datetime(2026,10,26,3,0,tzinfo=UTC)

def fixture(t=ASOF, dip=-1.9, spotshare=52, recent_buy=60, futuresbuy=53, oi_pct=0.1, rise=0.45):
    bars=[]
    for i in range(48):
        stamp=t-timedelta(minutes=48*5-i*5)
        # 48 closed 5m prices, 2 most recent rise from local close low
        close=100 if i<45 else 100+rise*(i-44)/3
        buy=recent_buy if i>=45 else spotshare
        bars.append({'start_utc':iso(stamp),'close':close,
             'taker_buy_quote_usdt':buy*100,'taker_sell_quote_usdt':(100-buy)*100})
    spot={'collected_utc':iso(t),'assets':{}}
    fut={'generated_utc':iso(t),'assets':{}}
    for sym in ('BTC','ETH'):
        spot['assets'][sym+'USDT']={'spot':{'flow':{
            'last_closed_bar_utc':iso(t),'latest_close_usdt':bars[-1]['close'],
            'windows':{'1h':{'status':'ok','buy_share_quote_pct':spotshare,'end_utc':iso(t)},
                       '4h':{'status':'ok','price_change_pct':dip,'end_utc':iso(t)}},
            'last_5m_bars':copy.deepcopy(bars)},
            'depth':{'depth_bands':{'plus_minus_0.1pct':{'coverage_complete_bid':True,'coverage_complete_ask':True,'bid_ask_ratio':1.2}}}}}
        fut['assets'][sym]={'futures_flow':{'last_complete_utc':iso(t),
            'windows':{'1h':{'status':'ok','taker_buy_share_pct':futuresbuy,'end_utc':iso(t)}}},
            'open_interest':{'history_asof_utc':iso(t),'changes':{'1h':{'status':'ok','native_units_pct':oi_pct}}},
            'liquidations':{'windows':{'1h':{'status':'ok','end_utc':iso(t),'long_liquidations_usd':100000,'short_liquidations_usd':100}}}}
    return spot,fut


class MonitorTests(unittest.TestCase):
    def test_candidate_not_order(self):
        spot,fut=fixture();s=feature_snapshot(spot,fut,'BTC',ASOF)
        self.assertEqual(s['state'],'PAPER_CANDIDATE_UNVALIDATED')
        self.assertTrue(s['research_only']);self.assertTrue(s['not_an_order_or_buy_recommendation'])

    def test_sell_continues(self):
        spot,fut=fixture(spotshare=43,recent_buy=38,futuresbuy=43,rise=0)
        self.assertEqual(feature_snapshot(spot,fut,'ETH',ASOF)['state'],'SELLING_CONTINUES')

    def test_stale_oi_disables_candidate(self):
        spot,fut=fixture()
        fut['assets']['BTC']['open_interest']['history_asof_utc']=iso(ASOF-timedelta(minutes=75))
        result=feature_snapshot(spot,fut,'BTC',ASOF)
        self.assertEqual(result['state'],'DATA_UNAVAILABLE')
        self.assertIn('stale_or_future_oi_history',result['missing'])

    def test_misaligned_ohlc_window_disables_candidate(self):
        spot,fut=fixture()
        spot['assets']['BTCUSDT']['spot']['flow']['windows']['4h']['end_utc']=iso(ASOF-timedelta(minutes=10))
        result=feature_snapshot(spot,fut,'BTC',ASOF)
        self.assertEqual(result['state'],'DATA_UNAVAILABLE')
        self.assertIn('spot_windows_misaligned',result['missing'])

    def test_unsynchronized_fail_closed(self):
        spot,fut=fixture()
        fut['assets']['BTC']['futures_flow']['last_complete_utc']=iso(ASOF-timedelta(minutes=20))
        result=feature_snapshot(spot,fut,'BTC',ASOF)
        self.assertEqual(result['state'],'DATA_UNAVAILABLE')
        self.assertIn('unsynchronized_spot_futures',result['missing'])

    def test_stale_fail_closed(self):
        spot,fut=fixture(ASOF-timedelta(minutes=40))
        x=feature_snapshot(spot,fut,'BTC',ASOF)
        self.assertEqual(x['state'],'DATA_UNAVAILABLE')
        self.assertFalse(x['spot_price_observation_usable'])

    def test_future_dated_fail_closed(self):
        spot,fut=fixture(ASOF+timedelta(minutes=10))
        self.assertEqual(feature_snapshot(spot,fut,'BTC',ASOF)['state'],'DATA_UNAVAILABLE')

    def test_stale_spot_cannot_be_paper_mark(self):
        with tempfile.TemporaryDirectory() as td:
            spot,fut=fixture();pipeline(spot,fut,ASOF,td)
            second=ASOF+timedelta(minutes=15)
            spot,fut=fixture(second)
            spot['collected_utc']=iso(ASOF-timedelta(hours=2))
            r=pipeline(spot,fut,second,td)
            self.assertEqual(r['new_paper_entries'],0)
            self.assertEqual(r['states']['BTC']['state'],'DATA_UNAVAILABLE')
            self.assertFalse(r['states']['BTC']['spot_price_observation_usable'])

    def test_missing_oi_fail_closed(self):
        spot,fut=fixture()
        del fut['assets']['BTC']['open_interest']['changes']['1h']
        x=feature_snapshot(spot,fut,'BTC',ASOF)
        self.assertEqual(x['state'],'DATA_UNAVAILABLE')
        self.assertTrue(x['spot_price_observation_usable'])

    def test_invalid_depth_not_used_to_imply_absorption(self):
        spot,fut=fixture()
        d=spot['assets']['BTCUSDT']['spot']['depth']['depth_bands']['plus_minus_0.1pct']
        d.update({'coverage_complete_bid':False,'bid_ask_ratio':10000})
        x=feature_snapshot(spot,fut,'BTC',ASOF)
        self.assertEqual(x['state'],'PAPER_CANDIDATE_UNVALIDATED')
        self.assertIsNone(x['evidence']['depth_bid_ask_ratio_0_1pct_snapshot'])

    def test_same_candle_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            spot,fut=fixture();a=pipeline(spot,fut,ASOF,td);b=pipeline(spot,fut,ASOF+timedelta(minutes=5),td)
            self.assertEqual(a['new_snapshots_saved'],2)
            self.assertEqual(b['new_snapshots_saved'],0)
            snaps=parse_jsonl(Path(td)/'snapshots'/'2026-10-26.jsonl')
            self.assertEqual(len(snaps),2)
            self.assertEqual(snaps[0]['first_seen_utc'],iso(ASOF))

    def test_candidate_cohort_24h_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            for minutes in (0,15,30,45):
                t=ASOF+timedelta(minutes=minutes)
                spot,fut=fixture(t)
                pipeline(spot,fut,t,td)
            entries=parse_jsonl(Path(td)/'entries'/'2026-10-26.jsonl')
            self.assertEqual(len(entries),2) # one selected candidate per coin
            self.assertTrue(all(e['signal_utc']==iso(ASOF) for e in entries))

    def test_signal_does_not_use_same_bar_as_entry(self):
        with tempfile.TemporaryDirectory() as td:
            spot,fut=fixture();a=pipeline(spot,fut,ASOF,td)
            self.assertEqual(a['new_paper_entries'],0)
            spot2,fut2=fixture(ASOF+timedelta(minutes=15))
            b=pipeline(spot2,fut2,ASOF+timedelta(minutes=15),td)
            self.assertEqual(b['new_paper_entries'],2)
            entries=parse_jsonl(Path(td)/'entries'/'2026-10-26.jsonl')
            self.assertEqual(entries[0]['entry_utc'],iso(ASOF+timedelta(minutes=15)))
            self.assertNotEqual(entries[0]['entry_utc'],entries[0]['signal_utc'])
            self.assertEqual(entries[0]['paper_entry_status'],'ELIGIBLE_PAPER_REFERENCE')

    def test_outcome_only_after_horizon(self):
        with tempfile.TemporaryDirectory() as td:
            spot,fut=fixture();pipeline(spot,fut,ASOF,td)
            second=ASOF+timedelta(minutes=15)
            spot,fut=fixture(second);pipeline(spot,fut,second,td)
            self.assertEqual(parse_jsonl(Path(td)/'outcomes'/'2026-10-26.jsonl'),[])
            # 1h mark from entry, not from original signal.
            third=ASOF+timedelta(minutes=75)
            spot,fut=fixture(third)
            b=pipeline(spot,fut,third,td)
            self.assertEqual(b['new_forward_outcomes'],2)
            oc=parse_jsonl(Path(td)/'outcomes'/'2026-10-26.jsonl')
            self.assertTrue(all(r['horizon_hours']==1 and r['status']=='OBSERVED_PROXY' for r in oc))
            self.assertEqual(pipeline(spot,fut,third+timedelta(minutes=1),td)['new_forward_outcomes'],0)

    def test_unobserved_forward_window_marked_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            spot,fut=fixture();pipeline(spot,fut,ASOF,td)
            entry=ASOF+timedelta(minutes=15)
            spot,fut=fixture(entry);pipeline(spot,fut,entry,td)
            later=entry+timedelta(hours=2)
            spot,fut=fixture(later);out=pipeline(spot,fut,later,td)
            oc=parse_jsonl(Path(td)/'outcomes'/'2026-10-26.jsonl')
            self.assertTrue(any(o['status']=='UNAVAILABLE_MISSING_TIMELY_OBSERVATION' for o in oc))
            self.assertEqual(out['new_forward_outcomes'],2)

    def test_no_entry_if_first_available_snapshot_too_late(self):
        with tempfile.TemporaryDirectory() as td:
            spot,fut=fixture();pipeline(spot,fut,ASOF,td)
            later=ASOF+timedelta(minutes=60)
            spot,fut=fixture(later)
            pipeline(spot,fut,later,td)
            entries=parse_jsonl(Path(td)/'entries'/'2026-10-26.jsonl')
            self.assertTrue(all(e['paper_entry_status']=='UNAVAILABLE_NO_TIMELY_NEXT_OBSERVATION' for e in entries))
            self.assertEqual(len(entries),2)

    def test_bad_spot_timestamp_do_not_corrupt_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            spot,fut=fixture()
            spot['assets']['BTCUSDT']['spot']['flow']['last_closed_bar_utc']='not-a-date'
            result=pipeline(spot,fut,ASOF,td)
            self.assertEqual(result['states']['BTC']['state'],'DATA_UNAVAILABLE')
            self.assertEqual(result['new_snapshots_saved'],1)

    def test_failure_does_not_backfill_stale_as_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            spot,fut=fixture(ASOF-timedelta(minutes=40))
            out=pipeline(spot,fut,ASOF,td)
            self.assertTrue(all(x['state']=='DATA_UNAVAILABLE' for x in out['states'].values()))
            self.assertEqual(out['new_paper_entries'],0)

if __name__=='__main__':unittest.main()
