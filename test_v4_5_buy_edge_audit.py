import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from v4_5_buy_edge_audit import (evaluate, parse_time, iso, common_mark, spot_usable,
    resolved_arm, coverage_status, arm_mark, load_rows, HORIZONS)

UTC=timezone.utc
T=datetime(2026,10,27,3,0,tzinfo=UTC)


def add(root,kind,dt,data):
    p=Path(root)/kind/(dt.date().isoformat()+'.jsonl');p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('a') as f:f.write(json.dumps(data)+'\n')


def snap(t,price=100,symbol='BTC',delay=5,usable=True):
    return {'symbol':symbol,'snapshot_id':symbol+iso(t),'first_seen_utc':iso(t),
        'spot_bar_end_utc':iso(t-timedelta(minutes=delay)),
        'reference_close_usdt':price,'spot_price_observation_usable':usable}


def cohort(symbol='BTC',start=T):
    return {'cohort_id':symbol+'_cohort_1','symbol':symbol,'start_seen_utc':iso(start),
        'origin_snapshot_id':'shock_'+symbol}


def entry43(arm,status='NO_SIGNAL_WITHIN_4H', t=T+timedelta(minutes=15),price=100,symbol='BTC'):
    return {'cohort_id':symbol+'_cohort_1','symbol':symbol,'arm':arm,'status':status,
        'entry_first_seen_utc':iso(t),'entry_spot_bar_end_utc':iso(t-timedelta(minutes=5)),
        'entry_reference_price_usdt':price}


def v44_decs(symbol='BTC',start=T,stage='MIXED_WAIT'):
    out=[]
    for minute in (15,65,115,165,215):
        out.append({'decision_id':symbol+str(minute),'symbol':symbol,
                    'decision_recorded_utc':iso(start+timedelta(minutes=minute)),
                    'stage':stage,'linked_v4_3_cohort_id':symbol+'_cohort_1','entry_signal':False})
    return out


def seed(root,symbol='BTC',with_baseline=True,with_v44=True,with_mark=True):
    c=cohort(symbol);add(root,'comparison_v4_3/cohorts',T,c)
    if with_baseline:
        add(root,'comparison_v4_3/entries',T,entry43('DIP_NEXT','ENTRY_REFERENCE_OBSERVED',symbol=symbol))
    for arm in ('WATCH_NEXT','CONFIRM_NEXT'):
        add(root,'comparison_v4_3/entries',T,entry43(arm,symbol=symbol))
    if with_v44:
        for d in v44_decs(symbol):add(root,'market_state_v4_4/decisions',T,d)
    add(root,'snapshots',T,snap(T+timedelta(minutes=15),100,symbol))
    if with_mark:
        add(root,'snapshots',T,snap(T+timedelta(hours=4,minutes=5),102,symbol))


class V45ProtocolTests(unittest.TestCase):
    def bootstrap(self,root):
        r=evaluate(T-timedelta(minutes=1),root)
        self.assertEqual(r['status'],'BOOTSTRAPPED_NO_HISTORICAL_BACKFILL')

    def test_no_backfill_initial_run(self):
        with tempfile.TemporaryDirectory() as td:
            seed(td)
            r=evaluate(T+timedelta(hours=5),td)
            self.assertEqual(r['status'],'BOOTSTRAPPED_NO_HISTORICAL_BACKFILL')
            self.assertEqual(r['by_symbol']['BTC']['4h']['registered_final_episodes'],0)
            self.assertEqual(evaluate(T+timedelta(hours=6),td)['new_final_evaluations'],0)

    def test_dip_benchmark_vs_no_signal_cash_same_end(self):
        with tempfile.TemporaryDirectory() as td:
            self.bootstrap(td);seed(td)
            r=evaluate(T+timedelta(hours=5),td)
            self.assertEqual(r['new_final_evaluations'],1)
            summary=r['by_symbol']['BTC']['4h']
            self.assertEqual(summary['baseline_evaluable'],1)
            self.assertEqual(summary['arms']['DIP_NEXT']['mean_common_end_net_pct'],1.86)
            self.assertEqual(summary['arms']['WATCH_NEXT']['mean_common_end_net_pct'],0.)
            self.assertEqual(summary['arms']['WATCH_NEXT']['mean_delta_vs_dip_pp'],-1.86)
            self.assertEqual(summary['arms']['V44_SPOT_RECOVERY']['no_trade_count'],1)

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            self.bootstrap(td);seed(td)
            self.assertEqual(evaluate(T+timedelta(hours=5),td)['new_final_evaluations'],1)
            self.assertEqual(evaluate(T+timedelta(hours=5,minutes=1),td)['new_final_evaluations'],0)
            self.assertEqual(len(load_rows(Path(td)/'buy_edge_v4_5','evaluations',T+timedelta(hours=5))),1)

    def test_future_candle_cannot_be_common_end(self):
        now=T+timedelta(hours=5)
        snapshots=[snap(T+timedelta(hours=3,minutes=45),101),
                   snap(T+timedelta(hours=4,minutes=10),102,delay=15)]
        correct=[x for x in snapshots if spot_usable(x,now)]
        self.assertIsNone(common_mark(correct,'BTC',T+timedelta(hours=4),now))

    def test_missing_common_terminal_must_not_fake_return(self):
        with tempfile.TemporaryDirectory() as td:
            self.bootstrap(td);seed(td,with_mark=False)
            r=evaluate(T+timedelta(hours=5),td)
            self.assertEqual(r['new_final_evaluations'],0)
            r=evaluate(T+timedelta(hours=37),td)
            self.assertEqual(r['new_final_evaluations'],2)
            self.assertEqual(r['by_symbol']['BTC']['4h']['baseline_evaluable'],0)

    def test_missing_upstream_entry_not_cash(self):
        with tempfile.TemporaryDirectory() as td:
            self.bootstrap(td);seed(td,with_baseline=False)
            r=evaluate(T+timedelta(hours=5),td)
            self.assertEqual(r['new_final_evaluations'],0)
            r=evaluate(T+timedelta(hours=37),td)
            self.assertEqual(r['new_final_evaluations'],2)
            self.assertEqual(r['by_symbol']['BTC']['4h']['baseline_evaluable'],0)

    def test_v44_no_signal_requires_coverage(self):
        self.assertEqual(coverage_status(v44_decs(),'BTC',T),'OBSERVED_NO_SPOT_RECOVERY')
        self.assertEqual(coverage_status(v44_decs()[:1],'BTC',T),'INSUFFICIENT_V44_COVERAGE')
        broken=v44_decs();broken[2]['stage']='SOURCE_UNVERIFIED'
        self.assertEqual(coverage_status(broken,'BTC',T),'INSUFFICIENT_V44_COVERAGE')

    def test_v44_signal_requires_linked_paper_entry(self):
        c=cohort();decision={'decision_id':'D1','symbol':'BTC',
            'decision_recorded_utc':iso(T+timedelta(hours=1)),
            'linked_v4_3_cohort_id':c['cohort_id'],'entry_signal':True}
        a=resolved_arm(c,'V44_SPOT_RECOVERY',[],[decision],[])
        self.assertEqual(a['status'],'WAITING_FOR_V44_ENTRY')
        paper={'decision_id':'D1','status':'REFERENCE_ENTRY_OBSERVED',
            'entry_seen_utc':iso(T+timedelta(hours=1,minutes=20)),
            'entry_price_bar_end_utc':iso(T+timedelta(hours=1,minutes=15)),
            'entry_reference_usdt':100.5}
        self.assertEqual(resolved_arm(c,'V44_SPOT_RECOVERY',[],[decision],[paper])['status'],'ENTERED')

    def test_unlinked_v44_signal_is_not_falsely_treated_as_no_signal(self):
        c=cohort();decision={'decision_id':'D_unlinked','symbol':'BTC',
            'decision_recorded_utc':iso(T+timedelta(hours=1)),
            'linked_v4_3_cohort_id':None,'entry_signal':True}
        self.assertEqual(resolved_arm(c,'V44_SPOT_RECOVERY',[],
            v44_decs()+[decision],[])['status'],'AMBIGUOUS_UNLINKED_RECOVERY')

    def test_unlinked_other_symbol_does_not_block(self):
        c=cohort();decision={'decision_id':'D_eth','symbol':'ETH',
            'decision_recorded_utc':iso(T+timedelta(hours=1)),
            'linked_v4_3_cohort_id':None,'entry_signal':True}
        self.assertEqual(resolved_arm(c,'V44_SPOT_RECOVERY',[],
            v44_decs()+[decision],[])['status'],'OBSERVED_NO_SPOT_RECOVERY')

    def test_v44_cannot_enter_before_signal(self):
        c=cohort();decision={'decision_id':'D1','symbol':'BTC',
            'decision_recorded_utc':iso(T+timedelta(hours=1)),
            'linked_v4_3_cohort_id':c['cohort_id'],'entry_signal':True}
        paper={'decision_id':'D1','status':'REFERENCE_ENTRY_OBSERVED',
            'entry_seen_utc':iso(T+timedelta(minutes=30)),
            'entry_price_bar_end_utc':iso(T+timedelta(minutes=25)),
            'entry_reference_usdt':95}
        self.assertEqual(resolved_arm(c,'V44_SPOT_RECOVERY',[],[decision],[paper])['status'],'UNAVAILABLE_V44_ENTRY')

    def test_waiting_entry_after_common_end_is_cash_not_profit(self):
        end=snap(T+timedelta(hours=4,minutes=5),105)
        later={'status':'ENTERED','entry_seen_utc':iso(T+timedelta(hours=4,minutes=20)),
            'entry_reference_usdt':104}
        z=arm_mark(later,end,[snap(T+timedelta(hours=4,minutes=5),105)],T+timedelta(hours=5))
        self.assertEqual(z['status'],'CASH_ENTRY_AFTER_HORIZON')
        self.assertEqual(z['net_pct_by_cost']['0.14'],0)

    def test_sampled_mae_uses_observed_prices(self):
        a={'status':'ENTERED','entry_seen_utc':iso(T+timedelta(minutes=15)),
           'entry_reference_usdt':100}
        rows=[snap(T+timedelta(minutes=15),100),snap(T+timedelta(hours=1),94),
              snap(T+timedelta(hours=4,minutes=5),102)]
        m=arm_mark(a,rows[-1],rows,T+timedelta(hours=5))
        self.assertEqual(m['status'],'REFERENCE_HOLD_TO_COMMON_CLOCK')
        self.assertEqual(m['sampled_mae_pct'],-6)
        self.assertEqual(m['net_pct_by_cost']['0.14'],1.86)

    def test_24h_event_clock_uses_same_terminal_price_for_all(self):
        with tempfile.TemporaryDirectory() as td:
            self.bootstrap(td);seed(td)
            add(td,'snapshots',T+timedelta(hours=24,minutes=5),
                snap(T+timedelta(hours=24,minutes=5),110))
            r=evaluate(T+timedelta(hours=25),td)
            self.assertEqual(r['new_final_evaluations'],2)
            self.assertEqual(r['by_symbol']['BTC']['24h']['baseline_evaluable'],1)
            self.assertEqual(r['by_symbol']['BTC']['24h']['arms']['DIP_NEXT']['mean_common_end_net_pct'],9.86)
            self.assertEqual(r['by_symbol']['BTC']['24h']['arms']['WATCH_NEXT']['mean_common_end_net_pct'],0)

    def test_v44_recovery_candidate_is_paired_with_dip(self):
        with tempfile.TemporaryDirectory() as td:
            self.bootstrap(td);seed(td,with_v44=False)
            d={'decision_id':'RECOVERY123','symbol':'BTC',
                'decision_recorded_utc':iso(T+timedelta(minutes=65)),
                'linked_v4_3_cohort_id':'BTC_cohort_1', 'entry_signal':True,
                'stage':'SPOT_RECOVERY_PROXY'}
            add(td,'market_state_v4_4/decisions',T,d)
            e={'decision_id':'RECOVERY123','status':'REFERENCE_ENTRY_OBSERVED',
                'entry_seen_utc':iso(T+timedelta(minutes=80)),
                'entry_price_bar_end_utc':iso(T+timedelta(minutes=75)),
                'entry_reference_usdt':99}
            add(td,'market_state_v4_4/entries',T,e)
            add(td,'snapshots',T+timedelta(minutes=80),
                snap(T+timedelta(minutes=80),99))
            r=evaluate(T+timedelta(hours=5),td)
            report=r['by_symbol']['BTC']['4h']['arms']['V44_SPOT_RECOVERY']
            self.assertEqual(report['paired_episodes'],1)
            self.assertEqual(report['mean_common_end_net_pct'],2.8903)
            self.assertGreater(report['mean_delta_vs_dip_pp'],0)

    def test_24h_cannot_finalize_at_5h(self):
        with tempfile.TemporaryDirectory() as td:
            self.bootstrap(td);seed(td)
            r=evaluate(T+timedelta(hours=5),td)
            self.assertEqual(r['by_symbol']['BTC']['24h']['registered_final_episodes'],0)
            self.assertEqual(r['by_symbol']['BTC']['4h']['registered_final_episodes'],1)

    def test_event_specific_symbols(self):
        with tempfile.TemporaryDirectory() as td:
            self.bootstrap(td);seed(td,symbol='ETH')
            r=evaluate(T+timedelta(hours=5),td)
            self.assertEqual(r['by_symbol']['ETH']['4h']['baseline_evaluable'],1)
            self.assertEqual(r['by_symbol']['BTC']['4h']['baseline_evaluable'],0)

    def test_closed_bar_price_never_after_observation(self):
        invalid=snap(T,100,delay=-5)
        self.assertFalse(spot_usable(invalid,T+timedelta(minutes=10)))
        self.assertFalse(spot_usable(snap(T,100,delay=40),T+timedelta(minutes=10)))

    def test_late_entry_before_delayed_mark_is_still_cash(self):
        mark=snap(T+timedelta(hours=4,minutes=35),103)
        entry={'status':'ENTERED','entry_seen_utc':iso(T+timedelta(hours=4,minutes=20)),
               'entry_reference_usdt':102}
        r=arm_mark(entry,mark,[mark],T+timedelta(hours=5),T+timedelta(hours=4))
        self.assertEqual(r['status'],'CASH_ENTRY_AFTER_HORIZON')
        self.assertEqual(r['net_pct_by_cost']['0.35'],0)

    def test_entry_at_exact_common_cutoff_has_no_prior_position(self):
        mark=snap(T+timedelta(hours=4,minutes=10),103)
        e={'status':'ENTERED','entry_seen_utc':iso(T+timedelta(hours=4)), 'entry_reference_usdt':100}
        r=arm_mark(e,mark,[mark],T+timedelta(hours=5),T+timedelta(hours=4))
        self.assertEqual(r['status'],'CASH_ENTRY_AFTER_HORIZON')

    def test_cost_scenarios_separate(self):
        entry={'status':'ENTERED','entry_seen_utc':iso(T+timedelta(minutes=15)),
               'entry_reference_usdt':100}
        marks=[snap(T+timedelta(minutes=15)),snap(T+timedelta(hours=4,minutes=5),100)]
        r=arm_mark(entry,marks[-1],marks,T+timedelta(hours=5))
        self.assertEqual(r['net_pct_by_cost']['0.14'],-0.14)
        self.assertEqual(r['net_pct_by_cost']['0.35'],-0.35)


if __name__ == '__main__':unittest.main()
