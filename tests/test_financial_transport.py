import copy
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

from a_stock_agents.financial_transport import fetch_financial_json,FinancialUnavailable,classify
from a_stock_agents.errors import SourceBlockedError
from a_stock_agents.config import Settings,load_settings
from a_stock_agents.agent_tools import AgentTools
from a_stock_agents.storage import atomic_write_json


class FinancialTransportTests(unittest.TestCase):
    def test_transient_failure_retries_then_succeeds(self):
        fetch=Mock(side_effect=[TimeoutError('timed out'),{'success':True}])
        with patch('a_stock_agents.financial_transport.time.sleep'):
            data,attempts=fetch_financial_json(fetch,'https://example.com',{})
        self.assertTrue(data['success']);self.assertEqual(len(attempts),2);self.assertEqual(attempts[0]['category'],'timeout')

    def test_curl_only_after_two_transport_failures(self):
        fetch=Mock(side_effect=SourceBlockedError('SSL disconnected'))
        result=SimpleNamespace(returncode=0,stdout=b'{"success":true}\n200')
        with patch('a_stock_agents.financial_transport.time.sleep'),patch('a_stock_agents.financial_transport.subprocess.run',return_value=result) as curl:
            data,attempts=fetch_financial_json(fetch,'https://example.com',{})
        self.assertEqual(fetch.call_count,2);self.assertTrue(data['success']);self.assertEqual(attempts[-1]['transport'],'curl');curl.assert_called_once()

    def test_denials_and_rate_limits_not_bypassed(self):
        for status,category in [(403,'access_denied'),(429,'rate_limited')]:
            fetch=Mock(side_effect=urllib.error.HTTPError('https://example.com',status,'denied',None,None))
            with patch('a_stock_agents.financial_transport.subprocess.run') as curl:
                with self.assertRaises(FinancialUnavailable) as caught:fetch_financial_json(fetch,'https://example.com',{})
            self.assertEqual(caught.exception.category,category);self.assertEqual(fetch.call_count,1);curl.assert_not_called()

    def test_invalid_curl_response_does_not_become_data(self):
        with patch('a_stock_agents.financial_transport.time.sleep'),patch('a_stock_agents.financial_transport.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=b'not json\n200')):
            with self.assertRaises(FinancialUnavailable):fetch_financial_json(Mock(side_effect=TimeoutError()),'https://example.com',{})

    def test_wrapped_errors_keep_reason(self):
        try:
            try:raise urllib.error.HTTPError('https://example.com',403,'denied',None,None)
            except Exception as exc:raise SourceBlockedError('wrapped') from exc
        except Exception as exc:self.assertEqual(classify(exc),('access_denied',403))

    def test_cache_only_same_stock_cutoff_and_valid_age(self):
        with tempfile.TemporaryDirectory() as folder:
            values=copy.deepcopy(load_settings().values);values['research']['enabled']=False
            tools=AgentTools(Settings(Path(folder),values))
            response={'success':True,'result':{'data':[{'SECURITY_CODE':'001309','NOTICE_DATE':'2026-08-22','REPORTDATE':'2026-06-30'}]}}
            try:
                with patch.object(tools,'http') as http:
                    http.return_value.get_json.return_value=response
                    first=tools.get_financials('001309','2026-10-22');second=tools.get_financials('001309','2026-10-22')
                    self.assertFalse(first['cached']);self.assertTrue(second['cached']);self.assertEqual(http.return_value.get_json.call_count,1)
                    tools.get_financials('001309','2026-10-21');self.assertEqual(http.return_value.get_json.call_count,2)
                stale={**first,'fetched_at':'2000-01-01T00:00:00+00:00'}
                atomic_write_json(tools.settings.runtime/'agents/fundamental/cache/001309_2026-10-22.json',stale)
                with patch.object(tools,'http') as http,patch('a_stock_agents.financial_transport.time.sleep'),patch('a_stock_agents.financial_transport.subprocess.run',side_effect=OSError('network')):
                    http.return_value.get_json.side_effect=TimeoutError()
                    with self.assertRaises(FinancialUnavailable):tools.get_financials('001309','2026-10-22')
            finally:tools.close()


if __name__=='__main__':unittest.main()
