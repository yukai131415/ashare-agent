import copy
import json
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import Mock,patch
from a_stock_agents.config import Settings,load_settings
from a_stock_agents.intraday import run_once,records,targets,watchlist,view,session_slot,next_slot,set_enabled,validate_quote
from a_stock_agents.scheduler import SHANGHAI


DAY='2026-10-26'
def at(time):return datetime.fromisoformat(DAY+'T'+time+':00+08:00')
STOCK={'code':'001309','name':'测试股票','recommendation_date':'2026-10-23','target_date':DAY,'reference_price':10,'sources':['正式推荐','影子实验']}


class IntradayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        values=copy.deepcopy(load_settings().values);values['llm']={'enabled':True,'model':'test-model','api_key':'unit-test-only','base_url':'https://invalid.example/v1'}
        self.settings=Settings(Path(self.tmp.name),values)
        self.pick=patch('a_stock_agents.intraday.targets',return_value=[copy.deepcopy(STOCK)]);self.pick.start();self.addCleanup(self.pick.stop)
        self.transport=Mock(side_effect=self.respond)

    def respond(self,cfg,messages,tools,force,timeout):
        self.assertTrue(force)
        args={'summary':'仅作证据观察','signals':[{'observation':'有当前快照','interpretation':'存在多种可能','alternative':'也可能为普通成交','evidence_ids':['quote:current']}],
              'risk_level':'unknown','watchpoints':['等待后续快照'],'limitations':['无逐笔委托数据']}
        call={'function':{'name':'submit_review','arguments':json.dumps(args)}}
        return {},call,{'input_tokens':123,'output_tokens':45}

    def quote(self,time='09:30',price=10,amount=10000):
        return {'code':STOCK['code'],'name':'测试股票','price':price,'change_pct':1,'amount':amount,'turnover_pct':2,'quote_time':at(time).strftime('%Y%m%d%H%M%S')}

    def test_sessions_holidays_lunch_and_next_slot(self):
        self.assertIsNone(session_slot(at('09:29')))
        self.assertEqual(session_slot(at('09:39')),at('09:30'))
        self.assertEqual(session_slot(at('11:30')),at('11:30'))
        self.assertIsNone(session_slot(at('11:31')));self.assertIsNone(session_slot(at('12:50')))
        self.assertEqual(session_slot(at('13:00')),at('13:00'))
        self.assertIsNone(session_slot(at('15:01')))
        self.assertIsNone(session_slot(datetime.fromisoformat('2026-10-25T09:30:00+08:00')))
        self.assertIsNone(session_slot(datetime.fromisoformat('2026-10-01T09:30:00+08:00')))
        self.assertEqual(next_slot(at('11:31')),at('13:00').isoformat())
        self.assertEqual(next_slot(at('09:30')),at('09:40').isoformat())

    def test_slot_dedup_usage_and_comparable_interval(self):
        fetch=Mock(return_value=self.quote())
        run_once(self.settings,at('09:30'),fetch,self.transport)
        run_once(self.settings,at('09:31'),fetch,self.transport)
        self.assertEqual(fetch.call_count,1);self.assertEqual(self.transport.call_count,1)
        fetch.return_value=self.quote('09:40',11,12000)
        run_once(self.settings,at('09:40'),fetch,self.transport)
        rows=records(self.settings,DAY)
        self.assertEqual(len(rows),2);self.assertTrue(all(r['status']=='completed' for r in rows))
        self.assertEqual(rows[0]['derived']['interval_turnover_amount'],2000)
        self.assertAlmostEqual(rows[0]['derived']['price_change_since_previous_pct'],10)
        self.assertEqual(rows[0]['usage'],{'input_tokens':123,'output_tokens':45})
        self.assertEqual(rows[1]['derived'],{})
        with patch('a_stock_agents.intraday.shanghai_now',return_value=at('09:42')):
            self.assertNotIn('usage',view(self.settings,DAY)['records'][0])
            self.assertEqual(view(self.settings,DAY,True)['system_usage']['input_tokens'],246)

    def test_stale_quote_never_calls_model_and_failure_is_not_replayed(self):
        fetch=Mock(return_value=self.quote('09:30'))
        run_once(self.settings,at('09:40'),fetch,self.transport)
        row=records(self.settings,DAY)[0]
        self.assertEqual(row['status'],'quote_failed');self.transport.assert_not_called()
        run_once(self.settings,at('09:41'),fetch,self.transport)
        self.assertEqual(fetch.call_count,1)

    def test_lunch_disabled_and_no_watchlist_make_no_external_calls(self):
        fetch=Mock()
        run_once(self.settings,at('12:00'),fetch,self.transport)
        set_enabled(self.settings,False);run_once(self.settings,at('09:30'),fetch,self.transport)
        fetch.assert_not_called();self.transport.assert_not_called()
        set_enabled(self.settings,True)
        with patch('a_stock_agents.intraday.targets',return_value=[]):run_once(self.settings,at('09:30'),fetch,self.transport)
        fetch.assert_not_called()

    def test_model_error_keeps_quote_and_is_not_a_success(self):
        self.transport.side_effect=ValueError('HTTP 429 配额不足')
        run_once(self.settings,at('09:30'),Mock(return_value=self.quote()),self.transport)
        row=records(self.settings,DAY)[0]
        self.assertEqual(row['status'],'analysis_failed');self.assertIn('quote',row);self.assertNotIn('analysis',row)

    def test_watchlist_is_frozen_for_day_and_no_retroactive_slot_backfill(self):
        fetch=Mock(return_value=self.quote('10:20'))
        run_once(self.settings,at('10:20'),fetch,self.transport)
        with patch('a_stock_agents.intraday.targets',return_value=[]):self.assertEqual(len(watchlist(self.settings,DAY,True)),1)
        self.assertEqual([r['slot'] for r in records(self.settings,DAY)],[at('10:20').isoformat()])

    def test_quote_wrong_code_future_and_nonfinite_are_rejected(self):
        for row in ({**self.quote(),'code':'600000'},self.quote('09:40'),{**self.quote(),'amount':float('nan')}):
            with self.assertRaises(ValueError):validate_quote(row,STOCK['code'],at('09:30'))

    def test_formal_and_shadow_sources_deduplicate_and_reject_same_day_selection(self):
        self.pick.stop()
        rec={**STOCK,'created_at':'2026-10-23T14:30:00+08:00'}
        shadow={'date':'2026-10-23','target_date':DAY,'started_at':'2026-10-25T14:30:00+08:00','status':'completed','selection':{'code':STOCK['code'],'name':'测试股票','price':10}}
        with patch('a_stock_agents.recommendation_store.recommendation_store',return_value=Mock(history=Mock(return_value=[rec]))),patch('a_stock_agents.shadow.history',return_value=[shadow]):
            rows=targets(self.settings,DAY);self.assertEqual(len(rows),1);self.assertEqual(set(rows[0]['sources']),{'正式推荐','影子实验'})
            shadow['started_at']='2026-10-26T09:00:00+08:00';rec['created_at']='2026-10-26T09:00:00+08:00'
            self.assertEqual(targets(self.settings,DAY),[])


if __name__=='__main__':unittest.main()
