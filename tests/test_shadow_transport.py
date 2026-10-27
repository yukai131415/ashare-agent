import io
import json
import unittest
import urllib.error
from unittest.mock import Mock,patch

from a_stock_agents.harness.adapters.llm import complete
from a_stock_agents.llm import LLMHTTPError


class ShadowTransportTests(unittest.TestCase):
    def setUp(self):
        self.config={'model':'gpt-5.6-luna','base_url':'https://api.openai.com/v1',
                     'api_key':'unit-test-secret','proxy_url':'http://127.0.0.1:1100'}

    def test_auth_error_is_readable_and_redacts_echoed_key(self):
        body=json.dumps({'error':{'message':'bad unit-test-secret sk-proj-***abcd'}}).encode()
        opener=Mock();opener.open.side_effect=urllib.error.HTTPError('https://api.openai.com/v1/responses',401,'Unauthorized',{},io.BytesIO(body))
        with patch('a_stock_agents.harness.adapters.llm.urllib.request.build_opener',return_value=opener):
            with self.assertRaises(LLMHTTPError) as caught:complete(self.config,[],[])
        self.assertIn('HTTP 401',str(caught.exception))
        self.assertNotIn('unit-test-secret',str(caught.exception));self.assertNotIn('abcd',str(caught.exception))
        self.assertEqual(opener.open.call_count,1)

    def test_configured_proxy_is_used_and_network_error_is_actionable(self):
        opener=Mock();opener.open.side_effect=urllib.error.URLError('timed out')
        with patch('a_stock_agents.harness.adapters.llm.urllib.request.ProxyHandler') as handler,patch('a_stock_agents.harness.adapters.llm.urllib.request.build_opener',return_value=opener):
            with self.assertRaisesRegex(ValueError,'代理或网络'):complete(self.config,[],[])
        handler.assert_called_once_with({'http':self.config['proxy_url'],'https':self.config['proxy_url']})

    def test_qwen_uses_selected_aliyun_url_and_key_not_openai(self):
        config={**self.config,'model':'qwen3.8-max','base_url':'https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1','api_key':'qwen-test-key'}
        opener=Mock();opener.open.side_effect=urllib.error.HTTPError(config['base_url'],401,'Unauthorized',{},io.BytesIO(b'{"error":{"message":"bad key"}}'))
        with patch('a_stock_agents.harness.adapters.llm.urllib.request.build_opener',return_value=opener):
            with self.assertRaises(LLMHTTPError) as caught:complete(config,[],[])
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,config['base_url']+'/chat/completions')
        self.assertEqual(request.get_header('Authorization'),'Bearer qwen-test-key')
        self.assertNotIn('OpenAI',str(caught.exception));self.assertIn('千问',str(caught.exception))
        self.assertFalse(json.loads(request.data)['enable_thinking'])

    def test_incomplete_response_is_not_misreported_as_no_selection(self):
        response=Mock();response.read.return_value=b'{"status":"incomplete","incomplete_details":{"reason":"max_output_tokens"}}'
        opener=Mock();opener.open.return_value.__enter__=Mock(return_value=response);opener.open.return_value.__exit__=Mock(return_value=False)
        with patch('a_stock_agents.harness.adapters.llm.urllib.request.build_opener',return_value=opener):
            with self.assertRaisesRegex(ValueError,'max_output_tokens'):complete(self.config,[],[])


if __name__=='__main__':unittest.main()
