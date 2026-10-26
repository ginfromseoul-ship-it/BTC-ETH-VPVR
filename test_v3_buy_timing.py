"""No internet or market database required. Run python -m unittest -v test_v3_buy_timing.py"""
import unittest
from v3_buy_timing import classify,calc_features,first_hit,prefix

class V3UnitTests(unittest.TestCase):
    def feature(self,**kwargs):
        x={'price_reclaim':True,'spot_1h_imbalance':.08,
           'spot_prior_1h_imbalance':.02,'spot_last15_imbalance':.04,
           'spot_improvement_pp':6.,'oi_valid':True,'oi_1h_change_pct':0.01,
           'risk_to_invalidation_pct_at_decision':1.2}
        x.update(kwargs)
        return x

    def test_strict_setup_known_information(self):
        self.assertEqual(classify(self.feature()),'BUY_SETUP_STRICT')

    def test_oi_missing_fails_closed_for_strict(self):
        self.assertEqual(classify(self.feature(oi_valid=False,oi_1h_change_pct=None)),'BUY_SETUP_EARLY')

    def test_reclaimed_price_is_mandatory_for_entry(self):
        self.assertEqual(classify(self.feature(price_reclaim=False)),'WATCH_NO_BUY')

    def test_spot_not_positive_means_no_entry(self):
        self.assertEqual(classify(self.feature(spot_1h_imbalance=-.01)),'WATCH_NO_BUY')

    def test_improving_not_sufficient_alone(self):
        self.assertEqual(classify(self.feature(spot_last15_imbalance=-.2)),'WATCH_NO_BUY')

    def test_risk_limit_blocks_entry(self):
        self.assertEqual(classify(self.feature(risk_to_invalidation_pct_at_decision=3.001)),'WATCH_NO_BUY')

    def test_strict_improvement_requirement(self):
        self.assertEqual(classify(self.feature(spot_improvement_pp=1.99)),'BUY_SETUP_EARLY')

    def test_missing_spot_is_unavailable(self):
        self.assertEqual(classify(self.feature(spot_last15_imbalance=None)),'NO_DATA')

    def test_continue_selling(self):
        self.assertEqual(classify(self.feature(price_reclaim=False,spot_1h_imbalance=-.1,spot_last15_imbalance=-.1)),'SELLING_NO_BUY')

    def test_conservative_same_bar_stop_first(self):
        rows=[(0,)*10+(130,85)]*289
        self.assertEqual(first_hit(rows,0,90,110),'stop')

    def test_future_rows_do_not_change_decision(self):
        rows=[]
        for j in range(100):
            price=99. if j<70 else 100.
            rows.append((j*300,(j+1)*300,price,price,5.,1.,100.,50.,10.,(j+1)*300-300,price*1.01,price*.99))
        p={'sv':prefix(rows,6),'sd':prefix(rows,4),'fv':prefix(rows,7),'fd':prefix(rows,5)}
        a=calc_features(rows,60,84,p)
        old=a.copy()
        for j in range(85,100):
            r=list(rows[j]);r[3]=1e6;r[8]=1e5;rows[j]=tuple(r)
        p2={'sv':prefix(rows,6),'sd':prefix(rows,4),'fv':prefix(rows,7),'fd':prefix(rows,5)}
        self.assertEqual(calc_features(rows,60,84,p2),old)

    def test_delayed_oi_blocks_unverified_value(self):
        rows=[]
        for j in range(100):
            rows.append((j*300,(j+1)*300,100.,100.,5.,1.,100.,50.,10.,(j+1)*300,101.,99.))
        p={'sv':prefix(rows,6),'sd':prefix(rows,4),'fv':prefix(rows,7),'fd':prefix(rows,5)}
        a=calc_features(rows,60,84,p)
        self.assertFalse(a['oi_valid'])

if __name__=='__main__':unittest.main()
