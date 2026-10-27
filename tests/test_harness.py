import copy
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock,patch
from a_stock_agents.config import Settings,load_settings
from a_stock_agents.errors import SourceBlockedError
from a_stock_agents.harness.planning.tool import execution_plan_tool,get_execution_plan
from a_stock_agents.harness.runtime import WorkflowHarness,ReviewRequired
from a_stock_agents.harness.persistence import RunStore
from a_stock_agents.harness.gateway import tool_boundary,model_boundary
from a_stock_agents.harness.policy import RecoveryPolicy,transient
from a_stock_agents.harness.evaluation import audit_run
from a_stock_agents.console_log import console_event as real_console_event


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.settings=Settings(Path(self.temp.name),copy.deepcopy(load_settings().values))
        quiet=patch('a_stock_agents.console_log.console_event');quiet.start();self.addCleanup(quiet.stop)

    def runtime(self,identifier='run',kind='formal',inputs=None):
        return WorkflowHarness(self.settings,identifier,kind,inputs or {})

    @staticmethod
    def success(identifier):return {'roles':{identifier:{'status':'success'}}}

    def complete_prefix(self,runtime,count=4):
        for specification in runtime.plan['steps'][:count]:
            name=specification['id'];runtime.run_step(name,{},lambda key=name:self.success(key))

    def test_plan_tool_is_typed_readonly_and_complete(self):
        for kind in ('formal','shadow','intraday'):
            plan=execution_plan_tool.invoke({'task_type':kind})
            self.assertTrue(all(step['acceptance'] for step in plan['steps']))
            for index,step in enumerate(plan['steps']):
                self.assertEqual(step['depends_on'],[plan['steps'][index-1]['id']] if index else [])
            plan['steps'].clear();self.assertTrue(get_execution_plan(kind)['steps'])
        with self.assertRaises(ValueError):execution_plan_tool.invoke({'task_type':'shell'})
        with self.assertRaises(ValueError):execution_plan_tool.invoke({'task_type':'formal','command':'anything'})

    def test_context_limit_is_not_a_token_budget(self):
        from a_stock_agents.harness.context import request_manifest
        result=request_manifest([{'role':'user','content':'你好'}],[])
        self.assertEqual(result['measurement'],'characters_not_tokens')
        with self.assertRaises(ValueError):request_manifest([{'role':'user','content':'x'*200}],[],maximum=100)

    def test_mcp_logging_does_not_pollute_stdout(self):
        import io
        from contextlib import redirect_stdout,redirect_stderr
        from a_stock_agents.console_log import console_output
        output,error=io.StringIO(),io.StringIO()
        with redirect_stdout(output),redirect_stderr(error),console_output(error):real_console_event('mcp_diagnostic')
        self.assertEqual(output.getvalue(),'');self.assertIn('mcp_diagnostic',error.getvalue())

    def test_successful_checkpoint_is_reused_after_restart(self):
        first=self.runtime();action=Mock(return_value=self.success('data_acquisition'))
        first.run_step('data_acquisition',{'snapshot':'frozen'},action)
        restarted=self.runtime();second=Mock(side_effect=AssertionError('duplicate execution'))
        output=restarted.run_step('data_acquisition',{'snapshot':'frozen'},second)
        self.assertEqual(output,self.success('data_acquisition'));second.assert_not_called()
        self.assertEqual(first.store.checkpoint('run','data_acquisition')['attempts'],1)

    def test_checkpoint_input_changes_are_rejected(self):
        runtime=self.runtime();runtime.run_step('data_acquisition',{'snapshot':'one'},lambda:self.success('data_acquisition'))
        with self.assertRaises(ReviewRequired):runtime.run_step('data_acquisition',{'snapshot':'two'},lambda:self.success('data_acquisition'))
        with self.assertRaises(ValueError):self.runtime(inputs={'other':'input'})

    def test_dependencies_and_incomplete_success_are_rejected(self):
        runtime=self.runtime()
        with self.assertRaises(ReviewRequired):runtime.run_step('risk',{},lambda:self.success('risk'))
        with self.assertRaises(ReviewRequired):runtime.finish(True)

    def test_uncertain_publication_cannot_be_replayed(self):
        runtime=self.runtime();self.complete_prefix(runtime)
        runtime.store.transition('run','decision','running')
        action=Mock()
        with self.assertRaises(ReviewRequired):runtime.run_step('decision',{},action)
        runtime.finish(False);action.assert_not_called()
        self.assertEqual(runtime.store.run('run')['status'],'needs_review')

    def test_verified_failure_never_becomes_cached_success(self):
        runtime=self.runtime()
        runtime.run_step('data_acquisition',{},lambda:{'roles':{'data_acquisition':{'status':'failed'}},'blocked':'bad data'})
        self.assertEqual(runtime.store.checkpoint('run','data_acquisition')['status'],'failed')
        runtime.run_step('data_acquisition',{},lambda:self.success('data_acquisition'))
        self.assertEqual(runtime.store.checkpoint('run','data_acquisition')['attempts'],2)

    def test_gateway_checks_permissions_and_archives_redacted_results(self):
        runtime=self.runtime(inputs={'as_of':'2026-10-23T14:30:00+08:00'})
        def execute():
            with self.assertRaises(PermissionError):
                with tool_boundary('sync_research',{}):self.fail('unauthorized execution')
            with self.assertRaises(PermissionError):
                with tool_boundary('get_market_snapshot',{'as_of':'2026-10-24'}):self.fail('future data')
            with tool_boundary('get_market_snapshot',{'api_key':'must-not-leak'}) as artifact:artifact['result']={'rows':[{'code':'600000'}]}
            return self.success('data_acquisition')
        with runtime.scope():runtime.run_step('data_acquisition',{},execute)
        with runtime.store.connection() as db:rows=db.execute('SELECT payload FROM tool_calls').fetchall()
        self.assertEqual(len(rows),1);self.assertNotIn('must-not-leak',rows[0][0]);self.assertIn('600000',rows[0][0])

    def test_ambiguous_model_timeout_blocks_replay(self):
        runtime=self.runtime()
        def execute():
            with model_boundary({'model':'qwen'}) as call:
                call['_mark_sent']();raise TimeoutError('may already have been processed')
        with runtime.scope(),self.assertRaises(TimeoutError):runtime.run_step('data_acquisition',{},execute)
        self.assertTrue(runtime.store.uncertain_model_calls('run','data_acquisition'))
        with self.assertRaises(ReviewRequired):runtime.run_step('data_acquisition',{},Mock())
        self.assertFalse(audit_run(self.settings,'run')['structural_checks_passed'])

    def test_model_received_usage_is_not_lost_when_validation_fails(self):
        runtime=self.runtime()
        def execute():
            with model_boundary({'model':'qwen'}) as receipt:
                receipt.update(status='received',provider_request_id='provider-id',usage={'input_tokens':12})
                raise ValueError('invalid final decision')
        with runtime.scope(),self.assertRaises(ValueError):runtime.run_step('data_acquisition',{},execute)
        trace=runtime.store.trace('run')
        self.assertEqual(trace['model_calls'][0]['status'],'received')
        self.assertEqual(json.loads(trace['model_calls'][0]['payload'])['usage']['input_tokens'],12)

    def test_retry_policy_is_structural_and_fail_closed(self):
        self.assertTrue(transient(TimeoutError()))
        self.assertTrue(transient(SourceBlockedError('HTTP 429')))
        self.assertFalse(transient(SourceBlockedError('unknown changed schema')))
        self.assertFalse(transient(SourceBlockedError('HTTP 404')))
        for status in (401,403,404):self.assertFalse(transient(urllib.error.HTTPError('url',status,'error',{},None)))
        with self.assertRaises(ValueError):RecoveryPolicy(retry_http_statuses=[401])
        with self.assertRaises(ValueError):RecoveryPolicy(max_worker_retries=99)

    def test_cumulative_recovery_budget_is_persistent(self):
        runtime=self.runtime()
        self.assertTrue(runtime.store.reserve_recovery('run',1))
        self.assertFalse(self.runtime().store.reserve_recovery('run',1))

    def test_audit_of_full_verified_run(self):
        runtime=self.runtime();self.complete_prefix(runtime,5);runtime.finish(True)
        self.assertTrue(audit_run(self.settings,'run')['structural_checks_passed'])

    def test_daily_scheduler_stops_repeated_pipeline_retries(self):
        from a_stock_agents.scheduler import DailyPipelineScheduler,SHANGHAI,scheduler_display
        from a_stock_agents.storage import atomic_write_json,read_json
        scheduler=DailyPipelineScheduler(self.settings,{'enabled':True,'hour':14,'minute':30})
        now=datetime(2026,10,22,14,40,tzinfo=SHANGHAI)
        atomic_write_json(scheduler.path,{'attempt_date':'2026-10-22','attempts':3,'status':'retry_wait'})
        with patch('a_stock_agents.scheduler.Supervisor') as supervisor:
            self.assertFalse(scheduler.run_once('test',now));supervisor.assert_not_called()
        state=read_json(scheduler.path)
        self.assertEqual(state['status'],'needs_review')
        display=scheduler_display({'enabled':True,'hour':14,'minute':30},state,now)
        self.assertEqual(display['countdown_state'],'needs_review')
