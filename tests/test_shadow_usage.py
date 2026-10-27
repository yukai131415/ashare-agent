import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from a_stock_agents.token_usage import shadow_usage_statistics


class ShadowUsageTests(unittest.TestCase):
    def test_attempts_dates_and_missing_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            settings=SimpleNamespace(runtime=Path(temp))
            self.assertEqual(shadow_usage_statistics(settings,{})['totals']['experiments'],0)
            folder=Path(temp)/'shadow';folder.mkdir()
            report={'date':'2026-10-23','started_at':'2026-10-25T12:00:00+08:00','model':'qwen','status':'completed','attempt':3,
                    'usage':{'input_tokens':100,'output_tokens':20,'model_calls':4,'unreported_responses':1},
                    'previous_attempts':[{'started_at':'2026-10-24T12:00:00+08:00','status':'failed','attempt':1,'usage':{'input_tokens':30,'output_tokens':5,'model_calls':2}},
                                         {'started_at':'2026-10-25T11:00:00+08:00','status':'failed','attempt':2,'usage':{}}]}
            with sqlite3.connect(folder/'experiments.db') as db:
                db.execute('CREATE TABLE experiments(payload TEXT)');db.execute('INSERT INTO experiments VALUES(?)',(json.dumps(report),))
            db.close()
            archive=folder/'archive';archive.mkdir();(archive/'duplicate.json').write_text(json.dumps(report))
            result=shadow_usage_statistics(settings,{'tenant_id':['other']})
            self.assertEqual(result['totals']['input_tokens'],130)
            self.assertEqual(result['totals']['experiments'],3)
            self.assertEqual(result['totals']['missing_usage_attempts'],1)
            self.assertIsNone(result['details'][1]['input_tokens'])
            filtered=shadow_usage_statistics(settings,{'from':['2026-10-25'],'to':['2026-10-25']})
            self.assertEqual(filtered['totals']['experiments'],2)
            self.assertEqual(filtered['totals']['input_tokens'],100)
            self.assertEqual(filtered['details'][1]['model'],'历史记录未保存')
