import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from a_stock_agents.config import Settings,load_settings
from a_stock_agents.shadow import Experiment


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.settings=Settings(Path(self.tmp.name),copy.deepcopy(load_settings().values))
        self.pool=[{'code':f'{i:06d}','name':f'股票{i}','amount':100-i,'change_pct':i,'price':10,'quote_time':'20261023143000'} for i in range(1,41)]
        self.report={'date':'2026-10-23','cutoff':'2026-10-23T14:32:00+08:00','pool':self.pool,'tool_calls':[],'reviews':{},'status':'running','market_scope':'on_demand'}
        self.exp=Experiment(self.settings,self.report,{},Mock());self.addCleanup(self.exp.tools.close)

    def test_can_find_outside_original_top12_and_page_deterministically(self):
        result=self.exp.tool('search_market',{'sort_by':'change_pct','limit':2})
        self.assertEqual([r['code'] for r in result['rows']],['000040','000039'])
        self.assertEqual(result['universe_count'],40);self.assertTrue(result['has_more'])
        self.assertEqual(self.exp.observed,{'000040','000039'})
        page=self.exp.tool('search_market',{'sort_by':'change_pct','limit':2,'offset':2})
        self.assertEqual(page['rows'][0]['code'],'000038')

    def test_unseen_codes_and_invalid_queries_are_rejected(self):
        args={'candidates':['000040'],'vetoes':[],'selected_code':'','summary':'test','risks':[],'evidence_ids':['snapshot:000040']}
        with self.assertRaises(ValueError):self.exp.validate_review('fundamental',args,{})
        for query in ({'limit':999},{'sort_by':'secret_score'},{'min_amount':float('nan')},{'url':'https://invalid.test'}):
            with self.assertRaises(ValueError):self.exp.tool('search_market',query)

    def test_large_token_usage_does_not_force_early_submission(self):
        self.exp.tool('search_market',{'query':'000040','limit':1})
        self.exp.usage['input_tokens']=50000
        def transport(config,messages,tools,force,timeout):
            self.assertFalse(force);self.assertIn('search_market',[t['function']['name'] for t in tools])
            call={'id':'submit','function':{'name':'submit_review','arguments':json.dumps({'candidates':['000040'],'vetoes':[],'selected_code':'','summary':'已有证据可提交','risks':[],'evidence_ids':['snapshot:000040']})}}
            return {'role':'assistant','tool_calls':[call]},call,{'input_tokens':100,'output_tokens':100}
        self.exp.transport=transport
        self.exp.role('technical',{'reviews':{}})
        self.assertEqual(self.exp.calls,1)

    def test_multidimensional_prefetch_is_bounded_and_prompt_does_not_dump_universe(self):
        with patch.object(self.exp.tools,'call',return_value={'status':'ok','reports':[{'PARENT_NETPROFIT':100}]}) as call:
            self.exp.prepare_financials()
            self.assertGreater(call.call_count,0);self.assertLessEqual(call.call_count,20)
        requests=[]
        def transport(config,messages,tools,force,timeout):
            requests.append(json.loads(messages[1]['content']))
            if len(requests)==1:
                call={'id':'query','function':{'name':'search_market','arguments':'{"query":"000040","limit":1}'}}
            else:
                call={'id':'submit','function':{'name':'submit_review','arguments':json.dumps({'candidates':['000040'],'vetoes':[],'selected_code':'','summary':'based on fetched snapshot','risks':['财报尚未核实'],'evidence_ids':['snapshot:000040']})}}
            return {'role':'assistant','tool_calls':[call]},call,{'input_tokens':10,'output_tokens':10}
        self.exp.transport=transport
        review=self.exp.role('fundamental',{'reviews':{}})
        self.assertEqual(requests[0]['pool'],[]);self.assertEqual(requests[0]['universe_count'],40)
        self.assertEqual(review['reviews']['fundamental']['candidates'],['000040'])

    def test_four_dimensions_deduplicate_and_positive_pe_asc_finance_once(self):
        for i,row in enumerate(self.pool,1):row.update(pe_ratio=i,turnover_pct=100-abs(i-20),volume_ratio=1)
        with patch.object(self.exp.tools,'call',return_value={'status':'ok','reports':[{'PARENT_NETPROFIT':100}]}) as call:
            self.exp.prepare_financials()
        coverage=self.report['exploration_coverage']
        self.assertEqual({d['field'] for d in coverage['dimensions']},{'amount','change_pct','turnover_pct','pe_ratio'})
        self.assertEqual(coverage['dimensions'][-1]['codes'],['000001','000002','000003','000004','000005'])
        self.assertEqual(call.call_count,len(set(coverage['candidate_codes'])))
        self.assertEqual(set(coverage['financial_ok']),set(coverage['candidate_codes']))
        self.assertEqual(len(coverage['risk_checks']),len(coverage['candidate_codes']))
        self.assertGreater(len(coverage['candidate_codes']),5)

    def test_decision_requires_dimension_and_financial_coverage(self):
        self.exp.tool('search_market',{'query':'000040','limit':1})
        args={'candidates':['000040'],'vetoes':[],'selected_code':'000040','summary':'test','risks':[],'evidence_ids':['snapshot:000040']}
        with self.assertRaisesRegex(ValueError,'多维检索'):self.exp.validate_review('decision',args,{'reviews':{}})
        self.report['exploration_coverage']={'dimensions':[{}, {}, {}, {}],'financial_ok':[]}
        with self.assertRaisesRegex(ValueError,'财务'):self.exp.validate_review('decision',args,{'reviews':{}})
