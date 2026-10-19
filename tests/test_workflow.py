import copy
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, Mock

from a_stock_agents.config import Settings,load_settings
from a_stock_agents.contracts import AgentResult,iso_now
from a_stock_agents.storage import ensure_runtime,write_csv,atomic_write_json
from a_stock_agents.agents.analysis import AnalysisAgent
from a_stock_agents.agents.recommendation import RecommendationAgent
from a_stock_agents.recommendation_store import recommendation_store
from a_stock_agents.workflow import AgentWorkflow
from a_stock_agents.agent_tools import AgentTools
from a_stock_agents.research import ResearchIndex
from a_stock_agents.supervisor import Supervisor


ROWS=[dict(code='600000',name='甲股票',price=10,change_pct=1,amount=2e8,volume_ratio=1,turnover_pct=2,pe_ratio=10,amplitude_pct=2,quote_time='20261019150000'),
      dict(code='000823',name='乙股票',price=20,change_pct=5,amount=8e8,volume_ratio=3,turnover_pct=10,pe_ratio=20,amplitude_pct=4,quote_time='20261019150000')]
NOW=datetime.fromisoformat('2026-10-19T15:05:00+08:00')


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        values=copy.deepcopy(load_settings().values)
        values['supervisor']['min_market_rows']=1
        values['workflow']['financials_enabled']=False
        values['research']['enabled']=False
        self.settings=Settings(Path(self.folder.name),values)
        ensure_runtime(self.settings.runtime)

    def runner(self,name):
        if name=='analysis':return AnalysisAgent(self.settings).run()
        if name=='recommendation':return RecommendationAgent(self.settings).run()
        if name=='market':
            write_csv(self.settings.runtime/'agents/market/data/latest.csv',ROWS,list(ROWS[0]))
        if name=='news':atomic_write_json(self.settings.runtime/'agents/news/data/latest.json',{'items':[]})
        return AgentResult(name,True,iso_now(),iso_now(),records=2)

    def invoke_live(self,workflow):
        with patch('a_stock_agents.workflow.datetime') as clock,patch('a_stock_agents.agents.recommendation.datetime') as rec_clock,patch('a_stock_agents.agents.analysis.iso_now',return_value=NOW.isoformat()):
            clock.now.return_value=NOW
            rec_clock.now.return_value=NOW
            rec_clock.fromisoformat.side_effect=datetime.fromisoformat
            rec_clock.strptime.side_effect=datetime.strptime
            return workflow.invoke('test_live',as_of=NOW.isoformat())

    def test_actual_graph_publishes_original_engine_pick_and_preserves_daily_lock(self):
        report,_=self.invoke_live(AgentWorkflow(self.settings,self.runner))
        self.assertTrue(report['ok'],report.get('blocked'))
        self.assertEqual(list(report['roles']),['data_acquisition','fundamental','technical','risk','decision'])
        self.assertEqual(report['recommendation']['code'],'000823')
        rec=report['recommendation']
        # Lock another eligible name: graph must defer to the existing RecommendationAgent lock.
        recommendation_store(self.settings.runtime).save({**rec,'code':'600000','name':'甲股票'})
        report,_=self.invoke_live(AgentWorkflow(self.settings,self.runner))
        self.assertEqual(report['recommendation']['code'],'600000')
        self.assertTrue(report['roles']['decision']['daily_lock_preserved'])
        self.assertEqual(report['roles']['risk']['reviewed_code'],'600000')

    def test_failed_collectors_stop_graph_without_old_snapshot_recommendation(self):
        def fail(name):return AgentResult(name,False,iso_now(),iso_now(),error_type='SourceBlockedError')
        report,_=AgentWorkflow(self.settings,fail).invoke('failure',persist=False)
        self.assertFalse(report['ok'])
        self.assertEqual(report['roles']['decision']['status'],'skipped')
        self.assertIsNone(report['recommendation'])

    def test_supervisor_runs_graph_and_keeps_legacy_health_contract(self):
        supervisor=Supervisor(self.settings)
        with patch.object(supervisor,'run_agent',side_effect=self.runner),patch('a_stock_agents.supervisor.is_trading_weekday',return_value=True),patch('a_stock_agents.workflow.datetime') as clock,patch('a_stock_agents.agents.recommendation.datetime') as rec_clock,patch('a_stock_agents.agents.analysis.iso_now',return_value=NOW.isoformat()):
            clock.now.return_value=NOW
            rec_clock.now.return_value=NOW
            rec_clock.strptime.side_effect=datetime.strptime
            rec_clock.fromisoformat.side_effect=datetime.fromisoformat
            report=supervisor.run_pipeline()
        self.assertTrue(report['ok'],report)
        self.assertEqual(report['workflow']['engine'],'langgraph')
        self.assertEqual(set(report['agents']),{'market','news','analysis','backtest','recommendation'})
        self.assertTrue(supervisor.health_path.exists())

    def test_replay_does_not_write_recommendation_database_or_mutate_inputs(self):
        rows=copy.deepcopy(ROWS)
        report,ranked=AgentWorkflow(self.settings).invoke('replay',replay_rows=rows,persist=False)
        self.assertTrue(report['ok'])
        self.assertEqual(rows,ROWS)
        self.assertEqual(report['recommendation']['code'],ranked[0][1]['code'])
        self.assertFalse((self.settings.runtime/'agents/recommendation/data/recommendations.db').exists())

    def test_stale_live_quotes_block_persistence(self):
        stale=[{**r,'quote_time':'20261016150000'} for r in ROWS]
        with patch(__name__+'.ROWS',stale):
            report,_=self.invoke_live(AgentWorkflow(self.settings,self.runner))
        self.assertFalse(report['ok'])
        self.assertEqual(report['roles']['risk']['status'],'failed')
        self.assertNotIn('recommendation',report['agents'])

    def test_financial_advisory_failure_does_not_change_pick(self):
        self.settings.values['workflow']['financials_enabled']=True
        with patch.object(AgentTools,'get_financials',side_effect=TimeoutError('network')):
            report,_=self.invoke_live(AgentWorkflow(self.settings,self.runner))
        self.assertTrue(report['ok'],report.get('blocked'))
        self.assertEqual(report['recommendation']['code'],'000823')
        self.assertEqual(report['roles']['fundamental']['evidence'][0]['financials']['status'],'unavailable')

    def test_tools_validate_input_before_network_and_filter_future_disclosures(self):
        tools=AgentTools(self.settings)
        self.assertTrue(any(t['function']['name']=='get_financials' for t in tools.schemas()))
        response=tools.dispatch_function_call({'function':{'name':'search_research','arguments':'{"query":"test","code":"000823","as_of":"2026-10-19T14:30:00+08:00"}'}})
        self.assertEqual(response['status'],'disabled')
        with self.assertRaises(ValueError):tools.call('get_financials',{'code':'bad','as_of':'2026-10-19'})
        response={'success':True,'result':{'data':[
            {'SECURITY_CODE':'000823','NOTICE_DATE':'2026-10-20','REPORTDATE':'2026-09-30'},
            {'SECURITY_CODE':'000823','NOTICE_DATE':'2026-08-22','REPORTDATE':'2026-06-30'},
            {'SECURITY_CODE':'600000','NOTICE_DATE':'2026-08-22'}]}}
        with patch.object(AgentTools,'http') as http:
            http.return_value.get_json.return_value=response
            r=tools.call('get_financials',{'code':'000823','as_of':'2026-10-19'})
        self.assertEqual(len(r['reports']),1)
        self.assertEqual(r['reports'][0]['REPORTDATE'],'2026-06-30')

    def test_milvus_query_carries_stock_and_time_filter(self):
        self.settings.values['research']['enabled']=True
        index=ResearchIndex(self.settings)
        fake=Mock()
        fake.has_collection.return_value=True
        fake.search.return_value=[[{'id':'a','distance':0.9,'entity':{'title':'测试研报','source':'test','text':'文本'}}]]
        index._client=fake
        result=index.search('盈利','000823','2026-10-19T14:30:00+08:00')
        self.assertEqual(result['hits'][0]['source'],'test')
        query=fake.search.call_args.kwargs
        self.assertIn('code == "000823"',query['filter'])
        self.assertIn('published_at <=',query['filter'])
        self.assertEqual(len(query['data'][0]),384)
        with self.assertRaises(ValueError):index.timestamp('2026-10-19')

    def test_milvus_ingestion_has_stable_chunk_ids_and_provenance(self):
        index=ResearchIndex(self.settings)
        fake=Mock()
        docs=[{'code':'000823','title':'测试文档','source':'unit-test','published_at':'2026-10-16T15:00:00+08:00','text':'财务情况'*300}]
        with patch.object(index,'ensure_collection',return_value=fake):
            first=index.ingest(docs)
            ids=[x['id'] for x in fake.upsert.call_args.kwargs['data']]
            second=index.ingest(docs)
            self.assertEqual(ids,[x['id'] for x in fake.upsert.call_args.kwargs['data']])
            self.assertEqual(first,second)


if __name__=='__main__':unittest.main()
