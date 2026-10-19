import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from a_stock_agents.config import load_settings,Settings
from a_stock_agents.research_collector import ReportBodyParser,ResearchCollector


class ResearchCollectorTests(unittest.TestCase):
    def test_extracts_only_report_body_not_navigation(self):
        parser=ReportBodyParser()
        parser.feed('<div>导航</div><div id="ctx-content"><p>正文&amp;估值<br>风险</p><div><p>利润</p></div></div><div>广告</div>')
        self.assertIn('正文&估值',parser.text())
        self.assertNotIn('广告',parser.text())
        self.assertNotIn('导航',parser.text())

    def test_sync_filters_wrong_stock_and_future_date_and_ingests_real_body(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(Path(folder),load_settings().values)
            index=Mock();index.ingest.return_value={'chunks':2}
            collector=ResearchCollector(settings,index)
            collector.http=Mock()
            collector.http.get_json.return_value={'data':[
                {'stockCode':'000823','stockName':'超声电子','infoCode':'AP20261018001','title':'公开研报','publishDate':'2026-10-18 00:00:00.000'},
                {'stockCode':'000823','stockName':'超声电子','infoCode':'AP20261019002','title':'同日未知时间','publishDate':'2026-10-19 00:00:00.000'},
                {'stockCode':'600000','infoCode':'AP20261018003','publishDate':'2026-10-18 00:00:00.000'}]}
            collector.http.get_text.return_value='<div id="ctx-content"><p>超声电子 000823 '+('测试资料与风险，仅作合成单元测试。'*20)+'</p></div>'
            result=collector.sync('000823','2026-10-19T14:30:00+08:00')
            self.assertEqual(result['documents'],1)
            self.assertEqual(collector.http.get_text.call_count,1)
            doc=index.ingest.call_args.args[0][0]
            self.assertEqual(doc['published_at'],'2026-10-18T23:59:59+08:00')
            self.assertIn('infocode=AP20261018001',doc['source'])

    def test_startup_does_not_manage_remote_milvus(self):
        from a_stock_agents.milvus_service import ensure_local_milvus
        from unittest.mock import patch
        settings=load_settings()
        with patch.dict('os.environ',{'ASTOCK_MILVUS_URI':'https://milvus.example.com:19530'}):
            self.assertEqual(ensure_local_milvus(settings)['status'],'external')
