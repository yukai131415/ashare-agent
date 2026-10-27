import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from a_stock_agents.config import Settings,load_settings
from a_stock_agents.harness.control.master import MasterAgent,record,view,transient
from a_stock_agents.errors import SourceBlockedError,SchemaChangedError
from a_stock_agents.supervisor import Supervisor
from a_stock_agents.contracts import AgentResult,iso_now


class MasterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.settings=Settings(Path(self.temp.name),load_settings().values)

    def test_transient_gate(self):
        self.assertTrue(transient(TimeoutError()))
        self.assertTrue(transient(SourceBlockedError('HTTP 429')))
        self.assertFalse(transient(SourceBlockedError('HTTP 403')))
        self.assertFalse(transient(SchemaChangedError('changed')))
        self.assertFalse(transient(ValueError('invalid JSON')))
        self.assertFalse(transient(urllib.error.HTTPError('url',401,'bad',{},None)))

    def test_model_is_bounded_and_usage_recorded(self):
        def transport(*args):
            return {},{'function':{'name':'submit_review','arguments':json.dumps({'action':'retry','reason':'暂时网络故障'})}},{'prompt_tokens':12,'completion_tokens':3}
        master=MasterAgent(self.settings,'run',transport)
        with patch('a_stock_agents.harness.control.master.llm_config',return_value={'enabled':True,'api_key':'fake','model':'test'}):
            self.assertEqual(master.recover('market',TimeoutError()),'retry')
            # Model cannot request retry on a schema/business failure.
            self.assertEqual(master.recover('market',SchemaChangedError()),'stop')
        admin=view(self.settings,True);public=view(self.settings)
        self.assertTrue(any(r.get('input_tokens')==12 for r in admin['events']))
        self.assertTrue(all('reason' not in r and 'input_tokens' not in r for r in public['events']))
        self.assertEqual(next(r for r in admin['workers'] if r['worker']=='market')['status'],'needs_attention')

    def test_worker_recovery_verifies_real_success(self):
        supervisor=Supervisor(self.settings);calls=[]
        class Worker:
            def __init__(self,settings):pass
            def run(self):
                calls.append(1)
                if len(calls)==1:raise TimeoutError('timeout')
                return AgentResult(agent='news',ok=True,started_at=iso_now(),finished_at=iso_now())
        supervisor.agent_types['news']=Worker
        with patch.object(supervisor.master,'ask',side_effect=ValueError('no model')),patch('a_stock_agents.supervisor.time.sleep'):
            result=supervisor.run_agent('news')
        self.assertTrue(result.ok);self.assertEqual(len(calls),2)
        self.assertTrue(result.repair_action)
        control=view(self.settings)
        self.assertEqual(control['recovery_statistics'],{'attempts':1,'successes':1})
        self.assertEqual(next(r for r in control['workers'] if r['worker']=='news')['status'],'success')

    def test_unsafe_worker_failure_stops_and_recommendation_not_replayed(self):
        for name,error in [('market',SchemaChangedError()),('recommendation',TimeoutError())]:
            supervisor=Supervisor(self.settings);calls=[]
            class Worker:
                def __init__(self,settings):pass
                def run(self):calls.append(1);raise error
            supervisor.agent_types[name]=Worker
            with patch.object(supervisor.master,'ask',side_effect=ValueError('no model')):
                result=supervisor.run_agent(name)
            self.assertFalse(result.ok);self.assertEqual(len(calls),1)

    def test_stale_running_never_claimed_success(self):
        record(self.settings,'run','news','running')
        result=view(self.settings)
        self.assertEqual(result['workers'][0]['status'],'running')
        from a_stock_agents.harness.control.master import connection
        with connection(self.settings) as db:
            row=db.execute('SELECT id,payload FROM events').fetchone()
            payload=json.loads(row['payload']);payload['at']='2020-01-01T00:00:00+08:00'
            db.execute('UPDATE events SET payload=? WHERE id=?',(json.dumps(payload),row['id']))
        self.assertEqual(view(self.settings)['workers'][0]['status'],'unconfirmed')

    def test_recovery_counts_use_full_history_not_visible_event_limit(self):
        with patch('a_stock_agents.console_log.console_event'):
            record(self.settings,'one','news','retry_wait',action='retry')
            record(self.settings,'one','news','failed_attempt')
            record(self.settings,'one','news','retry_wait',action='retry')
            record(self.settings,'one','news','success')
            record(self.settings,'one','news','success')
            record(self.settings,'two','market','retry_wait',action='retry')
            record(self.settings,'two','market','needs_attention')
            record(self.settings,'two','market','success')
            for _ in range(105):record(self.settings,'later','master','usage',input_tokens=1)
        result=view(self.settings)
        self.assertEqual(result['recovery_statistics'],{'attempts':3,'successes':1})
        self.assertEqual(len(result['events']),100)
