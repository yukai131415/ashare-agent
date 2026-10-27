"""Disposable UI fixture. Fake quotes and fake model output; no production writes."""
import copy
import runpy
import sys
from unittest.mock import Mock,patch
sys.path.insert(0,'tests')
from test_intraday import IntradayTests,at
from a_stock_agents.intraday import view,run_once
from a_stock_agents.web import dashboard_data
from a_stock_agents.config import load_settings

case=IntradayTests();case.setUp()
original_view=view
try:
    for time,price,amount in [('09:30',10,10000),('09:40',10.2,14000),('09:50',10.1,17000)]:
        run_once(case.settings,at(time),Mock(return_value=case.quote(time,price,amount)),case.transport)
    def fixture_view(unused,day=None,admin=False):
        with patch('a_stock_agents.intraday.shanghai_now',return_value=at('09:55')):
            result=original_view(case.settings,day,admin)
        for stock in result['watchlist']:stock['name']='测试股票（仅界面验收）'
        for record in result['records']:record['target']['name']='测试股票（仅界面验收）'
        return result
    real_data=dashboard_data(load_settings())
    with patch('a_stock_agents.intraday.view',side_effect=fixture_view),patch('a_stock_agents.web.dashboard_data',return_value=real_data):
        runpy.run_path('tests/preview_auth.py',run_name='__main__')
finally:case.doCleanups()
