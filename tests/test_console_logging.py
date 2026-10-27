import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from a_stock_agents.console_log import console_event, log_scope
from a_stock_agents.config import Settings, load_settings
from a_stock_agents.llm import answer_question, _curl_completion
from a_stock_agents.workflow import Role


class ConsoleLoggingTests(unittest.TestCase):
    def setUp(self):
        # Existing event-contract assertions use JSON; format is tested separately below.
        self.formatter=patch('a_stock_agents.console_log.format_log_line',side_effect=lambda entry:json.dumps(entry,ensure_ascii=False))
        self.formatter.start();self.addCleanup(self.formatter.stop)
    def test_secret_redaction_and_scope_reset(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            with log_scope(request_id='one',secrets=('private-key-value',)):
                console_event('test', prompt='hello private-key-value', nested={'Authorization':'Bearer abc','api_key':'abc'}, url='https://user:pass@example.com/?api_key=xyz')
            console_event('next')
        lines=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertNotIn('private-key-value',output.getvalue())
        self.assertNotIn('user:pass',output.getvalue())
        self.assertNotIn('xyz',output.getvalue())
        self.assertEqual(lines[0]['nested']['api_key'],'[REDACTED]')
        self.assertNotIn('request_id',lines[1])

    def test_prompt_response_usage_and_correlation_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(Path(folder),load_settings().values)
            config={'enabled':True,'api_key':'private-key-value','base_url':'https://example.com/v1','model':'test-model'}
            response=MagicMock()
            response.__enter__.return_value.read.return_value=json.dumps({'choices':[{'message':{'content':'评分为 1.23'}}], 'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}}).encode()
            opener=MagicMock();opener.open.return_value=response
            output=io.StringIO()
            with patch('a_stock_agents.llm.llm_config',return_value=config),patch('a_stock_agents.llm.urllib.request.build_opener',return_value=opener),contextlib.redirect_stdout(output):
                answer,source,_=answer_question(settings,'为什么推荐？','{"score":1.23}','本地')
            lines=[json.loads(line) for line in output.getvalue().splitlines()]
            request=next(x for x in lines if x['event']=='llm_request')
            result=next(x for x in lines if x['event']=='llm_response')
            self.assertEqual((answer,source),('评分为 1.23','llm'))
            self.assertIn('为什么推荐？',request['request']['messages'][-1]['content'])
            self.assertEqual(result['usage']['total_tokens'],15)
            self.assertEqual(request['request_id'],result['request_id'])
            self.assertNotIn('private-key-value',output.getvalue())

    def test_agent_returned_failure_is_not_logged_as_success(self):
        class FailedRole(Role):
            name,title='test','测试'
            def execute(self,state):
                return {'blocked':'no data'},{'status':'failed','market_rows':0}
        workflow=MagicMock()
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            result=FailedRole(workflow)({'run_id':'test-run'})
        lines=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(lines[-1]['status'],'failed')
        self.assertEqual(lines[-1]['summary']['market_rows'],0)
        self.assertFalse(workflow.event.call_args.args[0]['ok'])
        self.assertEqual(result['blocked'],'no data')

    def test_curl_response_usage_and_transport_failure(self):
        output=io.StringIO()
        completed=MagicMock(returncode=0,stdout=json.dumps({'choices':[{'message':{'content':'test'}}],'usage':{'total_tokens':4}}))
        with patch('a_stock_agents.llm.subprocess.run',return_value=completed),contextlib.redirect_stdout(output),log_scope(request_id='curl-test',secrets=('fake-secret',)):
            answer=_curl_completion('https://example.com/v1/chat/completions',b'{"model":"test","messages":[]}',{'api_key':'fake-secret'},'openai_compatible')
        self.assertEqual(answer,'test')
        lines=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(lines[-1]['usage']['total_tokens'],4)
        self.assertEqual(lines[-1]['request_id'],'curl-test')
        self.assertNotIn('fake-secret',output.getvalue())
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(Path(folder),load_settings().values)
            output=io.StringIO()
            config={'enabled':True,'api_key':'fake-secret','base_url':'https://example.com/v1','model':'test'}
            with patch('a_stock_agents.llm.llm_config',return_value=config),patch('a_stock_agents.llm._urllib_completion',side_effect=OSError('fake-secret failure')),patch('a_stock_agents.llm._curl_completion',side_effect=OSError('fake-secret failure')),contextlib.redirect_stdout(output):
                result=answer_question(settings,'question','{}','local')
            lines=[json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(result[1],'fallback')
            self.assertIn('answer_transport_failed',[line['event'] for line in lines])
            self.assertNotIn('llm_response',[line['event'] for line in lines])
            self.assertNotIn('fake-secret',output.getvalue())


if __name__=='__main__':
    unittest.main()
