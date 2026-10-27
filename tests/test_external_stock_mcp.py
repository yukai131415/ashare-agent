import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from a_stock_agents.config import Settings, load_settings
from a_stock_agents.external_stock_mcp import request_spec, parse_result, stock_observation, _cache
from a_stock_agents.web import page_html


def response(text, error=False):
    return SimpleNamespace(isError=error, content=[SimpleNamespace(type='text', text=text)])


class StockMCPTests(unittest.TestCase):
    def test_fixed_tools_and_validated_arguments(self):
        self.assertEqual(request_spec('funds', {'code':['000530']})[:2], ('stock_fund_flow', {'symbol':'000530'}))
        self.assertEqual(request_spec('limitpool', {'date':['2026-10-23'],'pool':['强势']})[0], 'stock_zt_pool')
        self.assertEqual(request_spec('billboard', {})[1], {'days':'5','limit':100})
        self.assertEqual(request_spec('sectorfunds', {})[1], {'days':'5日','cate':'行业资金流'})
        for kind,params in [('shell',{}),('funds',{'code':'../secret'}),('billboard',{'days':'999'}),('sectorfunds',{'category':'未知'}),('limitpool',{'date':'2026-10-24'}),('limitpool',{'date':'2099-01-01'})]:
            with self.assertRaises(ValueError):request_spec(kind,params)

    def test_csv_preserves_codes_quotes_and_units(self):
        result=parse_result(response('# 数据来源: 东方财富\n日期,代码,主力净流入（元）,名称\n2026-10-23,000530,123,"测试,名称"'))
        self.assertEqual(result['rows'][0]['代码'],'000530')
        self.assertEqual(result['rows'][0]['名称'],'测试,名称')
        self.assertEqual(result['data_dates'],['2026-10-23'])
        self.assertEqual(result['metadata'],['数据来源: 东方财富'])

    def test_failures_never_become_fake_rows(self):
        for body in ('获取龙虎榜数据失败','Not Found for 000530','','unknown response','a,b\n'):
            with self.assertRaises(ValueError):parse_result(response(body))
        with self.assertRaises(ValueError):parse_result(response('a,b\n1,2',True))

    def test_cache_and_no_stale_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(Path(folder),{})
            sample={'rows':[{'代码':'000530'}],'columns':['代码'],'metadata':[],'data_dates':[]}
            with patch('a_stock_agents.external_stock_mcp._call',new=AsyncMock(return_value=sample)) as call:
                first=stock_observation(settings,'funds',{})
                self.assertEqual(first['status'],'ok');self.assertFalse(first['cached'])
                self.assertTrue(stock_observation(settings,'funds',{})['cached']);self.assertEqual(call.await_count,1)
            _cache.clear()
            with patch('a_stock_agents.external_stock_mcp._call',new=AsyncMock(side_effect=RuntimeError('secret'))):
                failed=stock_observation(settings,'funds',{})
                self.assertEqual(failed['status'],'unavailable');self.assertEqual(failed['rows'],[])
                self.assertNotIn('secret',str(failed))

    def test_all_roles_have_four_menus_and_safe_renderer(self):
        html=page_html(load_settings())
        self.assertIn("['overview','brief','funds','limitpool','billboard','sectorfunds'].includes(key)",html)
        self.assertIn("fetch('/api/stock-observation?'",html)
        self.assertIn('node.textContent=String(value',html)
        self.assertIn('保留上游列名和原始单位',html)


if __name__=='__main__':unittest.main()
