from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from a_stock_agents.config import load_settings,Settings
from a_stock_agents.dashboard_enrichment import performance,equity_metrics,parse_candles,parse_sina_sectors
from a_stock_agents.models import MODELS,SCORING_VERSION
from a_stock_agents.research_dashboard import contributions,market_mood,feature_snapshot
from a_stock_agents.scheduler import SHANGHAI
from a_stock_agents.storage import atomic_write_json


class ResearchDashboardTests(unittest.TestCase):
    def test_contributions_exactly_match_all_original_models(self):
        for pe in [.2,10,None,-1]:
            f={'pe_ratio':pe,'momentum_z':1.2,'volume_ratio_z':-.4,'liquidity_z':2.,'turnover_z':3.,'valuation_z':100.,'amplitude_z':2.,'change_speed_z':-.2}
            for name,model in MODELS.items():
                result=contributions(f,name)
                self.assertAlmostEqual(result['total'],model.score(f),places=6)
                self.assertFalse(result['news_factor_enabled'])
        self.assertEqual(contributions({},'balanced','old')['status'],'unsupported_version')

    def test_mood_excludes_invalid_quotes_and_uses_yi_units(self):
        rows=[{'price':1,'change_pct':3,'amount':200000000,'volume_ratio':2},{'price':2,'change_pct':-5},{'price':2,'change_pct':0},{'price':0,'change_pct':10}]
        result=market_mood(rows,{})
        self.assertEqual((result['total'],result['up'],result['down'],result['flat']),(3,1,1,1))
        self.assertEqual(result['amount_yi'],2)
        self.assertEqual(sum(row['count'] for row in result['distribution']),3)
        self.assertEqual(market_mood([],None)['status'],'unavailable')
        missing=market_mood([{'price':1,'change_pct':0}],{})
        self.assertIsNone(missing['amount_yi'])
        self.assertIsNone(missing['active_volume_ratio_count'])

    def fixtures(self):
        rec=[{'date':'2026-10-19','target_date':'2026-10-20','code':'000530','name':'测试'}]
        def candle(c):return {'close':c,'high':c+1,'low':c-1,'open':c}
        series={'sz000530':{'fetched_at':'2026-10-20T15:15:00+08:00','candles':{'2026-10-19':candle(10),'2026-10-20':candle(11)}},'sh000300':{'fetched_at':'2026-10-20T15:15:00+08:00','candles':{'2026-10-19':candle(100),'2026-10-20':candle(102)}}}
        return rec,series

    def test_returns_costs_and_same_window_benchmark(self):
        records,series=self.fixtures()
        result=performance(records,series,now=datetime(2026,10,21,16,tzinfo=SHANGHAI))
        self.assertEqual(result['matched'],1)
        self.assertAlmostEqual(result['trades'][0]['net_return_pct'],19.,places=5)
        self.assertEqual(result['trades'][0]['exit_high'],12)
        charged=performance(records,series,.1,.1,now=datetime(2026,10,21,16,tzinfo=SHANGHAI))
        self.assertAlmostEqual(charged['trades'][0]['net_return_pct'],(1.19*.999/1.001-1)*100,places=5)
        self.assertEqual(result['summary']['benchmark_return_pct'],2.)

    def test_missing_and_intraday_are_not_zero_returns(self):
        records,series=self.fixtures()
        now=datetime(2026,10,20,14,30,tzinfo=SHANGHAI)
        result=performance(records,series,now=now)
        self.assertEqual((result['matched'],result['due']),(0,0))
        self.assertIsNone(result['summary'])
        series.pop('sh000300')
        result=performance(records,series,now=now.replace(hour=16))
        self.assertEqual(result['trades'][0]['status'],'missing')
        self.assertEqual(result['coverage_pct'],0)

    def test_custom_returns_are_compounded_in_curve_and_summary(self):
        records,series=self.fixtures()
        records.append({'date':'2026-10-20','target_date':'2026-10-21','code':'000530','name':'测试'})
        series['sz000530']['candles']['2026-10-21']={'open':11,'close':10,'high':12.1,'low':9}
        series['sh000300']['candles']['2026-10-21']={'open':102,'close':103,'high':104,'low':101}
        for snapshot in series.values():snapshot['fetched_at']='2026-10-21T15:15:00+08:00'
        result=performance(records,series,now=datetime(2026,10,21,16,tzinfo=SHANGHAI))
        self.assertEqual(result['matched'],2)
        self.assertAlmostEqual(result['summary']['compound_return_pct'],29.71)
        self.assertEqual(result['curve'][-1]['strategy_pct'],29.71)

    def test_one_price_day_not_assumed_filled(self):
        records,series=self.fixtures()
        series['sz000530']['candles']['2026-10-20'].update(high=11,low=11)
        result=performance(records,series,now=datetime(2026,10,21,16,tzinfo=SHANGHAI))
        self.assertEqual(result['trades'][0]['status'],'unverified_fill')

    def test_intraday_cache_never_becomes_a_close_after_clock_advances(self):
        records,series=self.fixtures()
        series['sz000530']['fetched_at']='2026-10-20T14:30:00+08:00'
        result=performance(records,series,now=datetime(2026,10,20,16,tzinfo=SHANGHAI))
        self.assertEqual(result['trades'][0]['status'],'missing_close')
        self.assertEqual(result['matched'],0)

    def test_drawdown_includes_initial_equity_and_streak(self):
        result=equity_metrics([-10,-10,5])
        self.assertAlmostEqual(result['max_drawdown_pct'],-19)
        self.assertEqual(result['max_losing_streak'],2)
        self.assertEqual(result['current_losing_streak'],0)

    def test_bad_candles_and_invalid_cost_rejected(self):
        with self.assertRaises(ValueError):parse_candles({'code':0,'data':{'sz000001':{'day':[['2026-10-19',10,11,9,10]]}}},'sz000001')
        with self.assertRaises(ValueError):performance([],{},float('nan'))

    def test_secondary_sector_source_does_not_invent_time_or_amount_units(self):
        rows=parse_sina_sectors('var S_Finance_bankuai_sinaindustry = {"new_test":"new_test,测试行业,8,10,1,2.5,1000,10000,sz000001,1,10,1,测试"};')
        self.assertEqual(rows[0]['change_pct'],2.5)
        self.assertIsNone(rows[0]['quote_time'])
        self.assertIsNone(rows[0]['amount_yi'])
        with self.assertRaises(ValueError):parse_sina_sectors('<html>blocked</html>')

    def test_snapshot_matches_original_factors_not_latest_file_time(self):
        with tempfile.TemporaryDirectory() as root:
            settings=Settings(Path(root),load_settings().values)
            rec={'recommendation_date':'2026-10-19','code':'000530','reference_price':10,'score':.28,'model':'balanced','model_version':SCORING_VERSION,'basis':{'momentum_z':1}}
            d=settings.runtime/'agents/analysis/data'
            atomic_write_json(d/'features_20261019_143000.json',{'generated_at':'2026-10-19T14:30:00+08:00','features':[{'code':'000530','price':10,'momentum_z':1}]})
            atomic_write_json(d/'latest.json',{'generated_at':'2026-10-20T14:30:00+08:00','features':[{'code':'000530','price':10,'momentum_z':2}]})
            source,rows=feature_snapshot(settings,rec,{})
            self.assertTrue(source['path'].endswith('143000.json'))
            self.assertEqual(rows[0]['momentum_z'],1)
