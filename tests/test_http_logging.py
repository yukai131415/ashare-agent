import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from a_stock_agents.auth import auth_store
from a_stock_agents.config import Settings
from a_stock_agents.web import DashboardHandler
from a_stock_agents.console_log import console_event,configure_console_file


class HTTPLoggingTests(unittest.TestCase):
    def setUp(self):
        formatter=patch('a_stock_agents.console_log.format_log_line',side_effect=lambda entry:json.dumps(entry,ensure_ascii=False))
        formatter.start();self.addCleanup(formatter.stop)
    def test_polling_only_silences_success_console_not_file_or_errors(self):
        from a_stock_agents.http_logging import HTTPLogging
        from http.server import BaseHTTPRequestHandler
        import a_stock_agents.console_log as logs
        class Routes(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(503 if 'fail' in self.path else 200);self.end_headers()
            def do_POST(self):
                self.send_response(200);self.end_headers()
            def log_message(self,*args):pass
        class Handler(HTTPLogging,Routes):pass
        with tempfile.TemporaryDirectory() as directory,patch.object(logs,'_file_handler',None):
            path=Path(directory)/'backend.jsonl';configure_console_file(path)
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            output=io.StringIO()
            try:
                with contextlib.redirect_stdout(output):
                    for method,url in [('GET','/api/agent-control'),('GET','/api/research-dashboard?date=2026-10-23'),('GET','/api/shadow?fail=1'),('POST','/api/shadow/start')]:
                        c=HTTPConnection('127.0.0.1',server.server_port);c.request(method,url)
                        r=c.getresponse();r.read();c.close()
                rows=[json.loads(line) for line in output.getvalue().splitlines()]
                self.assertFalse(any(r['path']=='/api/agent-control' for r in rows))
                self.assertFalse(any(r['path']=='/api/research-dashboard' for r in rows))
                self.assertTrue(any(r['path']=='/api/shadow' and r.get('status')==503 for r in rows))
                self.assertTrue(any(r['method']=='POST' and r['event']=='http_request_started' for r in rows))
                saved=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
                self.assertEqual(len(saved),8)
                self.assertTrue(any(r['path']=='/api/agent-control' and r.get('status')==200 for r in saved))
            finally:
                server.shutdown();server.server_close();thread.join();logs._file_handler.close();logs._file_handler=None

    def test_requests_errors_and_client_events_are_correlated_and_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(Path(directory),{'llm':{'enabled':False}})
            store=auth_store(settings);store.setup('logging-admin','Temporary-Password-123')
            raw,user=store.login('logging-admin','Temporary-Password-123')
            output=io.StringIO()
            server=ThreadingHTTPServer(('127.0.0.1',0),type('LogHandler',(DashboardHandler,),{'settings':settings}))
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            def request(path,body=None,authenticated=False,csrf=True):
                conn=HTTPConnection('127.0.0.1',server.server_port)
                headers={'X-Request-ID':'logging-test-123','Content-Type':'application/json'}
                if authenticated:headers['Cookie']='ashare_session='+raw
                if csrf:headers['X-CSRF-Token']=user['csrf_token']
                conn.request('GET' if body is None else 'POST',path,None if body is None else json.dumps(body),headers)
                response=conn.getresponse();result=response.status,response.getheader('X-Request-ID'),response.read();conn.close();return result
            with contextlib.redirect_stdout(output):
                thread.start()
                try:
                    self.assertEqual(request('/api/dashboard?api_key=never-log-me')[0],401)
                    event={'event':'fetch_failed','path':'/api/ask?password=never-log-me','related_request_id':'browser-request-123','password':'never-log-me'}
                    self.assertEqual(request('/api/client-events',event,True,False)[0],403)
                    self.assertEqual(request('/api/client-events',event,True)[0],202)
                    with patch('a_stock_agents.web.dashboard_data',side_effect=RuntimeError('test crash')):
                        status,identifier,body=request('/api/dashboard',authenticated=True)
                        self.assertEqual(status,500);self.assertEqual(json.loads(body)['request_id'],identifier)
                finally:server.shutdown();server.server_close();thread.join()
            events=[json.loads(line) for line in output.getvalue().splitlines()]
            # GET status starts are file-only; failed finishes remain visible.
            self.assertEqual(len([e for e in events if e['event']=='http_request_started']),2)
            self.assertEqual(len([e for e in events if e['event']=='http_request_finished']),4)
            client=next(e for e in events if e['event']=='frontend_event')
            self.assertEqual(client['related_request_id'],'browser-request-123')
            self.assertEqual(client['page_path'],'/api/ask')
            self.assertNotIn('never-log-me',output.getvalue());self.assertNotIn(raw,output.getvalue())
            self.assertNotIn(user['csrf_token'],output.getvalue())
            self.assertTrue(any(e['event']=='http_request_exception' for e in events))

    def test_rotating_file_receives_redacted_event(self):
        import a_stock_agents.console_log as logs
        with tempfile.TemporaryDirectory() as directory,patch.object(logs,'_file_handler',None):
            path=Path(directory)/'backend.jsonl';configure_console_file(path)
            try:
                console_event('file_test',password='not-in-log',csrf_token='not-in-log')
                data=path.read_text(encoding='utf-8')
                self.assertNotIn('not-in-log',data);self.assertIn('file_test',data)
                self.assertEqual(logs._file_handler.backupCount,5)
            finally:logs._file_handler.close();logs._file_handler=None


if __name__=='__main__':unittest.main()
