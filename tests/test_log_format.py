import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from a_stock_agents import console_log as logs


class StandardLogTests(unittest.TestCase):
    def test_real_console_and_both_archives(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(logs,'_file_handler',None):
            path=Path(temp)/'backend.jsonl';logs.configure_console_file(path)
            try:
                output=io.StringIO()
                with contextlib.redirect_stdout(output),logs.log_scope(component='http',request_id='test-id',secrets=('private-value',)):
                    logs.console_event('http_request_finished',method='GET',path='/api/example',status=500,message='private-value\nnext',request={'large':'full payload'})
                    logs.console_event('poll',_console=False)
                text=output.getvalue()
                self.assertRegex(text,r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} ERROR \[')
                self.assertIn('astock.http',text);self.assertIn('request_id=test-id',text)
                self.assertNotIn('private-value',text);self.assertIn('full payload',text)
                self.assertEqual(len(text.splitlines()),1)
                readable=path.with_suffix('.log').read_text(encoding='utf-8')
                self.assertIn(' - poll',readable)
                saved=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
                self.assertEqual(saved[0]['level'],'ERROR');self.assertEqual(saved[0]['request']['large'],'full payload')
                self.assertEqual(len(saved),2)
            finally:logs._file_handler.close();logs._file_handler=None

    def test_levels_and_timezone(self):
        for entry,level in [({'status':200},'INFO'),({'status':403},'WARN'),({'status':503},'ERROR'),({'status':'retry_wait'},'WARN'),({'ok':False},'ERROR')]:
            self.assertEqual(logs.event_level({'event':'test',**entry}),level)
        line=logs.format_log_line({'at':'2026-10-25T12:00:00+00:00','event':'test'})
        self.assertTrue(line.startswith('2026-10-25 20:00:00.000 INFO'))
