import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock,patch
from a_stock_agents.http import HttpClient
from a_stock_agents.console_log import console_event,external_operation


class ExternalLoggingTests(unittest.TestCase):
    def test_upstream_request_response_values_and_redaction(self):
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=b'{"price":10,"api_key":"must-not-leak"}'
        response.status=200;response.headers={'Content-Type':'application/json'}
        output=io.StringIO()
        with patch('urllib.request.urlopen',return_value=response),contextlib.redirect_stdout(output):
            result=HttpClient(1,'test',0).get_json('https://example.com/quote',{'code':'600000','api_key':'hidden-value'})
        text=output.getvalue()
        self.assertEqual(result['price'],10)
        self.assertIn('600000',text);self.assertIn('http_status=200',text);self.assertIn('price',text)
        self.assertIn('call_id=',text);self.assertIn('duration_ms=',text)
        self.assertNotIn('must-not-leak',text);self.assertNotIn('hidden-value',text)

    def test_exception_and_large_payload_are_visible(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(TimeoutError):
                with external_operation('test',endpoint='https://example.com'):raise TimeoutError('timed out')
            console_event('large_response',response={'data':'x'*9000})
        text=output.getvalue()
        self.assertIn('external_failed',text);self.assertIn('TimeoutError',text)
        self.assertIn('已截断',text);self.assertIn('ERROR',text)

    def test_python_warning_bridge(self):
        import logging
        from a_stock_agents.console_log import StandardLoggingBridge
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            StandardLoggingBridge().emit(logging.LogRecord('a_stock_agents.test',logging.WARNING,'',0,'source unavailable',(),None))
        self.assertIn('WARN',output.getvalue());self.assertIn('source unavailable',output.getvalue())
