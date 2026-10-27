import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch,Mock

from a_stock_agents.config import Settings,load_settings
from a_stock_agents.shadow import execute,history,view,pair_outcome,Experiment,freeze_input,set_enabled,config_view
from a_stock_agents.harness.adapters.llm import build_payload
PREPARE_FINANCIALS=Experiment.prepare_financials

POOL=[{'code':'600000','name':'甲','price':10,'amount':1e9,'quote_time':'20261023143000'},
      {'code':'000823','name':'乙','price':20,'amount':2e9,'quote_time':'20261023143000'}]
BASE={'code':'600000','name':'甲','recommendation_date':'2026-10-23','created_at':'2026-10-23T14:32:00+08:00','target_date':'2026-10-26','reference_price':10}


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        values=copy.deepcopy(load_settings().values);values['research']['enabled']=False
        self.settings=Settings(Path(self.tmp.name),values)
        self.config={'enabled':True,'api_key':'fake-only','base_url':'https://example.com/v1','model':'fake'}
        preload=patch.object(Experiment,'prepare_financials');self.preload=preload.start();self.addCleanup(preload.stop)

    def test_preloaded_financials_reach_all_roles_without_truncating_json(self):
        report={'date':'2026-10-23','cutoff':BASE['created_at'],'pool':POOL,'tool_calls':[],'reviews':{},'status':'running'}
        exp=Experiment(self.settings,report,self.config,self.transport)
        try:
            with patch.object(exp.tools,'call',return_value={'status':'ok','code':'000823','reports':[{'REPORTDATE':'2026-06-30','NOTICE_DATE':'2026-08-01','PARENT_NETPROFIT':123}]}):
                # Invoke original method despite suite-level network isolation.
                for row in POOL:exp.tool('get_financials',{'code':row['code']})
            captured=[]
            def transport(config,messages,tools,force,timeout):
                captured.append(json.loads(messages[1]['content']))
                return self.transport(config,messages,tools,force,timeout)
            exp.transport=transport
            exp.role('decision',{'reviews':{}})
            self.assertEqual(len(captured[0]['prior_evidence']),2)
            self.assertEqual(captured[0]['prior_evidence']['tool:1']['data']['reports'][0]['net_profit'],123)
        finally:exp.tools.close()

    def test_prefetch_queries_each_candidate_and_keeps_single_stock_failure(self):
        report={'date':'2026-10-23','cutoff':BASE['created_at'],'pool':POOL,'tool_calls':[],'reviews':{},'status':'running'}
        exp=Experiment(self.settings,report,self.config,self.transport)
        try:
            with patch.object(exp.tools,'call',side_effect=[TimeoutError(),{'status':'ok','reports':[]}]) as fetch:
                PREPARE_FINANCIALS(exp)
            self.assertEqual(fetch.call_count,2)
            self.assertEqual([r['status'] for r in report['tool_calls']],['failed','ok'])
            self.assertEqual(len(exp.evidence),3)
        finally:exp.tools.close()

    def test_repair_archives_old_abstention_and_is_one_time(self):
        from a_stock_agents.shadow import save,folder
        old={'date':'2026-10-23','status':'abstained','attempt':2,'baseline':BASE,'pool':POOL,'cutoff':BASE['created_at'],
             'tool_calls':[{'name':'get_financials','code':'000823','status':'failed'}],'usage':{'input_tokens':17},'reviews':{}}
        save(self.settings,old)
        with patch('a_stock_agents.shadow.llm_config',return_value=self.config):
            execute(self.settings,'2026-10-23',BASE,POOL,BASE['created_at'],self.transport,repair=True)
        report=history(self.settings)[0]
        self.assertEqual(report['status'],'completed');self.assertFalse(report['prospective'])
        self.assertEqual(report['repair_revision'],1)
        self.assertEqual(json.loads((folder(self.settings)/'archive/2026-10-23-before-evidence-repair.json').read_text(encoding='utf-8')),old)
        transport=Mock(side_effect=AssertionError('must not rerun'))
        execute(self.settings,'2026-10-23',BASE,POOL,BASE['created_at'],transport,repair=True)
        transport.assert_not_called()

    def transport(self,config,messages,tools,force,timeout):
        decision='影子决策' in messages[0]['content']
        review={'candidates':['000823'],'vetoes':[],'selected_code':'000823' if decision else '',
                'summary':'单元测试模型输出','risks':['不确定性'],'evidence_ids':['snapshot:000823']}
        call={'id':'mock','type':'function','function':{'name':'submit_review','arguments':json.dumps(review)}}
        return {'role':'assistant','tool_calls':[call]},call,{'input_tokens':10,'output_tokens':5}

    def run_mock(self,transport=None):
        with patch('a_stock_agents.shadow.llm_config',return_value=self.config):
            execute(self.settings,'2026-10-23',BASE,POOL,BASE['created_at'],transport or self.transport)
        return history(self.settings)[0]

    def test_separate_shadow_can_disagree_without_touching_production(self):
        path=self.settings.runtime/'agents/recommendation/data/latest.json';path.parent.mkdir(parents=True);path.write_text(json.dumps(BASE))
        before=path.read_bytes();report=self.run_mock()
        self.assertEqual(report['status'],'completed');self.assertEqual(report['selection']['code'],'000823')
        self.assertEqual(report['baseline']['code'],'600000');self.assertEqual(path.read_bytes(),before)
        public_pool=view(self.settings)['reports'][0]['candidate_pool']
        self.assertEqual(len(public_pool),2)
        self.assertEqual(set(public_pool[0]),{'code','name','price'})
        self.assertEqual(list(report['reviews']),['fundamental','technical','risk','decision'])
        self.assertEqual(report['usage']['input_tokens'],40);self.assertEqual(report['usage']['output_tokens'],20)
        self.assertNotIn('usage',view(self.settings)['reports'][0]);self.assertIn('system_usage',view(self.settings,True))

    def test_success_is_immutable_and_failure_does_not_fake_selection(self):
        self.run_mock()
        transport=Mock(side_effect=AssertionError('must not run'))
        self.run_mock(transport);transport.assert_not_called()

    def test_manual_model_refresh_archives_and_uses_selected_config(self):
        first=self.run_mock()
        observed=[]
        def selected_transport(config,*args):
            observed.append(config['model']);return self.transport(config,*args)
        execute(self.settings,'2026-10-23',BASE,POOL,BASE['created_at'],selected_transport,
                manual_config={**self.config,'model':'selected-test-model','_profile_id':'test-profile'})
        result=history(self.settings)[0]
        self.assertEqual(observed,['selected-test-model']*4)
        self.assertEqual(result['model'],'selected-test-model');self.assertFalse(result['prospective'])
        self.assertTrue(result['manual_refresh']);self.assertEqual(result['attempt'],2)
        archived=list((self.settings.runtime/'shadow/archive').glob('2026-10-23-*.json'))
        self.assertEqual(len(archived),1)
        self.assertEqual(json.loads(archived[0].read_text(encoding='utf-8')),first)
        self.assertEqual(view(self.settings,True)['system_usage']['input_tokens'],80)

    def test_model_options_are_admin_only_and_contain_no_secrets(self):
        with patch('a_stock_agents.shadow.llm_config',return_value=self.config):
            public=view(self.settings);admin=view(self.settings,True)
        self.assertNotIn('model_options',public)
        self.assertEqual(admin['model_options'][0]['model'],'fake')
        self.assertNotIn('fake-only',json.dumps(admin,ensure_ascii=False))

    def test_refresh_uses_profile_credentials_and_rejects_unknown_profile(self):
        from a_stock_agents.shadow import start_shadow
        profile={'model':'selected','base_url':'https://selected.example/v1','api_key':'','api_key_env':'SHADOW_TEST_KEY','proxy_url':''}
        def thread_factory(*args,**kwargs):return Mock(start=Mock(side_effect=kwargs['target']))
        with patch('a_stock_agents.shadow.llm_config',return_value=self.config),patch('a_stock_agents.shadow.model_options',return_value=({'profile':profile},'profile')),patch.dict('os.environ',{'SHADOW_TEST_KEY':'selected-secret'}),patch('a_stock_agents.shadow.freeze_input',return_value=('2026-10-23',BASE,POOL,BASE['created_at'])),patch('a_stock_agents.shadow.execute') as run,patch('a_stock_agents.shadow.threading.Thread',side_effect=thread_factory):
            with self.assertRaises(ValueError):start_shadow(self.settings,profile_id='unknown')
            run.assert_not_called()
            result=start_shadow(self.settings,profile_id='profile')
            self.assertTrue(result['started'])
            self.assertEqual(run.call_args.kwargs['manual_config']['api_key'],'selected-secret')
            self.assertEqual(run.call_args.kwargs['manual_config']['base_url'],profile['base_url'])
            self.assertEqual(self.config['model'],'fake')

    def test_out_of_pool_choice_rejected(self):
        def bad(config,messages,tools,force,timeout):
            message,call,usage=self.transport(config,messages,tools,force,timeout)
            data=json.loads(call['function']['arguments']);data['candidates']=['688001'];call['function']['arguments']=json.dumps(data)
            return message,call,usage
        report=self.run_mock(bad)
        self.assertEqual(report['status'],'failed');self.assertNotIn('selection',report)

    def test_risk_veto_blocks_final_choice(self):
        def bad(config,messages,tools,force,timeout):
            message,call,usage=self.transport(config,messages,tools,force,timeout)
            if '风险复核' in messages[0]['content']:
                data=json.loads(call['function']['arguments']);data['vetoes']=['000823'];call['function']['arguments']=json.dumps(data)
            return message,call,usage
        self.assertEqual(self.run_mock(bad)['status'],'failed')

    def test_model_failure_never_falls_back_to_formal_stock(self):
        report=self.run_mock(Mock(side_effect=TimeoutError()))
        self.assertEqual(report['status'],'failed');self.assertNotIn('selection',report)

    def test_retry_preserves_frozen_pool_and_previous_usage(self):
        counter=0
        def fail_after_first(config,messages,tools,force,timeout):
            nonlocal counter
            counter+=1
            if counter==2:raise TimeoutError()
            return self.transport(config,messages,tools,force,timeout)
        self.run_mock(fail_after_first)
        report=self.run_mock()
        self.assertEqual(report['attempt'],2);self.assertEqual(report['status'],'completed')
        self.assertEqual(view(self.settings,True)['system_usage']['input_tokens'],50)

    def test_reported_tokens_over_30000_do_not_stop_and_are_still_counted(self):
        def costly(config,messages,tools,force,timeout):
            message,call,_=self.transport(config,messages,tools,force,timeout)
            return message,call,{'input_tokens':30001,'output_tokens':1}
        report=self.run_mock(costly)
        self.assertEqual(report['status'],'completed');self.assertEqual(report['usage']['model_calls'],4)
        self.assertEqual(report['usage']['input_tokens'],120004)
        self.assertIsNone(config_view(self.settings)['max_reported_tokens'])

    def test_cumulative_characters_do_not_stop_but_oversized_request_is_rejected(self):
        report={'date':'2026-10-23','cutoff':BASE['created_at'],'pool':POOL,'tool_calls':[],'reviews':{},'status':'running'}
        exp=Experiment(self.settings,report,self.config,self.transport)
        try:
            exp.input_chars=250000
            result=exp.role('fundamental',{'reviews':{}})
            self.assertIn('fundamental',result['reviews']);self.assertGreater(exp.input_chars,250000)
            self.assertIsNone(config_view(self.settings)['max_input_characters'])
        finally:exp.tools.close()
        report={**report,'pool':[{**POOL[0],'name':'x'*110000}],'reviews':{}}
        transport=Mock();exp=Experiment(self.settings,report,self.config,transport)
        try:
            with self.assertRaisesRegex(ValueError,'单次模型请求过大'):exp.role('fundamental',{'reviews':{}})
            transport.assert_not_called();self.assertEqual(exp.calls,0)
        finally:exp.tools.close()

    def test_native_tool_loop_returns_evidence_then_submits(self):
        count=0
        def transport(config,messages,tools,force,timeout):
            nonlocal count
            count+=1
            if count==1:
                call={'id':'evidence','type':'function','function':{'name':'get_financials','arguments':'{"code":"000823"}'}}
                return {'role':'assistant','content':None,'tool_calls':[call]},call,{'prompt_tokens':12,'completion_tokens':3}
            self.assertEqual(messages[-1]['role'],'tool')
            return self.transport(config,messages,tools,force,timeout)
        report={'date':'2026-10-23','cutoff':BASE['created_at'],'pool':POOL,'tool_calls':[],'reviews':{},'status':'running'}
        exp=Experiment(self.settings,report,self.config,transport)
        try:
            with patch.object(exp.tools,'call',return_value={'status':'no_disclosed_data'}) as tool:
                result=exp.role('fundamental',{'reviews':{}})
                self.assertIn('fundamental',result['reviews'])
                self.assertEqual(tool.call_args.args[1]['as_of'],'2026-10-22')
        finally:exp.tools.close()

    def test_malformed_arguments_retry_once_without_executing_broken_tool(self):
        count=0
        def transport(config,messages,tools,force,timeout):
            nonlocal count
            count+=1
            if count==1:
                call={'id':'broken','function':{'name':'submit_review','arguments':'{"summary":"unfinished'}}
                return {'role':'assistant','tool_calls':[call]},call,{'input_tokens':10,'output_tokens':1000}
            self.assertTrue(force)
            return self.transport(config,messages,tools,force,timeout)
        report={'date':'2026-10-23','cutoff':BASE['created_at'],'pool':POOL,'tool_calls':[],'reviews':{},'status':'running'}
        exp=Experiment(self.settings,report,self.config,transport)
        try:
            with patch.object(exp.tools,'call') as tool:
                exp.role('fundamental',{'reviews':{}});tool.assert_not_called()
            self.assertEqual(count,2);self.assertEqual(exp.usage['output_tokens'],1005)
            self.assertEqual(report['stages']['fundamental']['status'],'completed')
        finally:exp.tools.close()

    def test_nondecision_selection_is_corrected_by_model_not_silently_rewritten(self):
        count=0
        def transport(config,messages,tools,force,timeout):
            nonlocal count
            count+=1
            schema=tools[-1]['function']['parameters']
            self.assertEqual(schema['properties']['selected_code']['enum'],[''])
            message,call,usage=self.transport(config,messages,tools,force,timeout)
            if count==1:
                args=json.loads(call['function']['arguments']);args['selected_code']='000823';call['function']['arguments']=json.dumps(args)
            else:
                self.assertTrue(force);self.assertEqual(messages[-1]['role'],'tool')
            return message,call,usage
        report={'date':'2026-10-23','cutoff':BASE['created_at'],'pool':POOL,'tool_calls':[],'reviews':{},'status':'running'}
        exp=Experiment(self.settings,report,self.config,transport)
        try:
            result=exp.role('fundamental',{'reviews':{}})
            self.assertEqual(count,2);self.assertEqual(result['reviews']['fundamental']['selected_code'],'')
            self.assertEqual(exp.usage['input_tokens'],20)
        finally:exp.tools.close()

    def test_public_snapshot_not_whole_private_features(self):
        from a_stock_agents.storage import atomic_write_json
        atomic_write_json(self.settings.runtime/'agents/recommendation/data/latest.json',BASE)
        features=[{**row,'momentum_z':99,'secret':'private'} for row in POOL]
        atomic_write_json(self.settings.runtime/'agents/analysis/data/latest.json',{'features':features})
        with patch('a_stock_agents.selection.rank_candidates',return_value=[(99,row) for row in features]):
            _,_,pool,_=freeze_input(self.settings)
        self.assertNotIn('momentum_z',json.dumps(pool));self.assertNotIn('secret',json.dumps(pool))

    def test_backtest_waits_for_close_and_excludes_retrospective(self):
        r={'date':'2026-10-23','target_date':'2026-10-26','baseline':BASE,'selection':POOL[1],'prospective':False}
        def snapshot(close,high):return {'fetched_at':'2026-10-26T15:15:00+08:00','candles':{'2026-10-23':{'close':close,'high':close+1,'low':close-1},'2026-10-26':{'high':high,'low':close-1}}}
        series={'sh600000':snapshot(10,11),'sz000823':snapshot(20,21)}
        now=datetime.fromisoformat('2026-10-26T14:30:00+08:00')
        self.assertEqual(pair_outcome(r,series,now)['status'],'pending')
        result=pair_outcome(r,series,now.replace(hour=16))
        self.assertEqual(result['status'],'ok');self.assertAlmostEqual(result['baseline_return_pct'],9)
        self.assertAlmostEqual(result['shadow_return_pct'],4);self.assertFalse(result['eligible_for_forward_statistics'])

    def test_native_chat_and_responses_protocols(self):
        tool={'type':'function','function':{'name':'submit_review','parameters':{'type':'object','properties':{}}}}
        endpoint,payload,provider=build_payload(self.config,[{'role':'user','content':'test'}],[tool],True)
        self.assertTrue(endpoint.endswith('/chat/completions'));self.assertEqual(payload['tool_choice']['function']['name'],'submit_review')
        config={**self.config,'base_url':'https://api.openai.com/v1'}
        endpoint,payload,provider=build_payload(config,[{'role':'assistant','response_items':[{'type':'function_call','name':'x','call_id':'1','arguments':'{}'}]},{'role':'tool','tool_call_id':'1','content':'{}'}],[tool])
        self.assertTrue(endpoint.endswith('/responses'));self.assertEqual(payload['input'][-1]['type'],'function_call_output')

    def test_admin_config_boolean_validation(self):
        set_enabled(self.settings,False);self.assertFalse(config_view(self.settings)['enabled'])
        with self.assertRaises(ValueError):set_enabled(self.settings,'false')

    def test_financial_recheck_adds_evidence_without_rewriting_conclusion(self):
        from a_stock_agents.shadow import save,recheck_financials
        report=self.run_mock();report['tool_calls']=[{'name':'get_financials','code':'600000','status':'failed'}];save(self.settings,report)
        coverage=view(self.settings)['reports'][0]['financial_coverage']
        self.assertEqual(coverage,{'pool':2,'queried':1,'failed':1,'not_queried':1})
        before=history(self.settings)
        response={'status':'ok','reports':[{'NOTICE_DATE':'2026-08-22'}],'source':'public','fetched_at':'2026-10-24T18:00:00+08:00'}
        with patch('a_stock_agents.agent_tools.AgentTools.call',return_value=response):
            result=recheck_financials(self.settings)
        self.assertEqual(result['checks'][0]['report_count'],1)
        self.assertEqual(history(self.settings),before)
        self.assertEqual(view(self.settings)['financial_recheck']['checks'][0]['status'],'ok')


if __name__=='__main__':unittest.main()
