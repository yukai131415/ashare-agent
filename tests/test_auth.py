import json
import tempfile
import threading
import unittest
from pathlib import Path
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch,MagicMock

from a_stock_agents.auth import AuthStore,AuthError,tenant_settings,auth_store,password_matches
from a_stock_agents.config import Settings
from a_stock_agents.token_usage import usage_actor,usage_call,report_usage,finish_usage,usage_statistics
from a_stock_agents.web import DashboardHandler,page_html


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.settings=Settings(Path(self.folder.name),{'llm':{'enabled':False,'api_key':'legacy-secret'}})
        self.store=auth_store(self.settings);self.admin=self.store.setup('administrator','Temporary-Password-123')
        self.tenant=self.store.create_tenant(self.admin,'第二租户')
        self.manager=self.store.create_user(self.admin,{'username':'manager','password':'Temporary-Password-123','role':'manager','tenant_id':self.tenant['id']})
        self.user=self.store.create_user(self.manager,{'username':'ordinary','password':'Temporary-Password-123'})

    def tearDown(self):self.folder.cleanup()

    def test_setup_only_once_password_hash_and_session_revocation(self):
        with self.assertRaises(AuthError):self.store.setup('another','Temporary-Password-123')
        with self.store.connection() as db:encoded=db.execute('SELECT password_hash FROM users WHERE id=?',(self.admin['id'],)).fetchone()[0]
        self.assertTrue(password_matches('Temporary-Password-123',encoded));self.assertNotIn('Temporary-Password',encoded)
        raw,p=self.store.login('ordinary','Temporary-Password-123');self.assertEqual(self.store.session(raw)['id'],p['id'])
        with self.store.connection() as db:self.assertNotEqual(db.execute('SELECT token_hash FROM sessions').fetchone()[0],raw)
        self.store.set_active(self.manager,self.user['id'],False);self.assertIsNone(self.store.session(raw))

    def test_manager_cannot_escalate_or_cross_tenant(self):
        for extra in ({'role':'admin'},{'role':'manager'},{'tenant_id':'default'}):
            with self.assertRaises(AuthError):self.store.create_user(self.manager,{'username':'forbidden','password':'Temporary-Password-123',**extra})
        self.assertTrue(all(u['tenant_id']==self.tenant['id'] for u in self.store.users(self.manager)))
        with self.assertRaises(AuthError):self.store.create_user(self.user,{'username':'forbidden','password':'Temporary-Password-123'})
        with self.assertRaises(AuthError):self.store.tenants(self.manager)
        with self.assertRaises(AuthError):self.store.set_active(self.manager,self.admin['id'],False)

    def test_tenant_config_isolated(self):
        private=tenant_settings(self.settings,self.tenant['id']);self.assertNotEqual(private.runtime,self.settings.runtime)
        self.assertEqual(private.values['llm']['api_key'],'');self.assertFalse(private.values['llm']['enabled'])
        self.assertEqual(self.settings.values['llm']['api_key'],'legacy-secret')

    def test_real_usage_unknown_retry_and_admin_only(self):
        with usage_actor(self.settings,self.user):
            with usage_call('request-one','qwen'):
                report_usage({'usage':{'prompt_tokens':120,'completion_tokens':32}},'openai','urllib')
                report_usage({'usage':{'prompt_tokens':120,'completion_tokens':32}},'openai','urllib')
                report_usage({'usage':{'input_tokens':10,'output_tokens':4}},'dashscope','curl');finish_usage('llm')
            with usage_call('request-two','qwen'):report_usage({},'openai','urllib');finish_usage('llm')
            with usage_call('request-local','rule'):finish_usage('local')
        result=usage_statistics(self.settings,self.admin,{})
        self.assertEqual(result['totals']['input_tokens'],130);self.assertEqual(result['totals']['output_tokens'],36)
        self.assertEqual(result['totals']['unreported_usage'],1);self.assertEqual(result['totals']['requests'],3)
        self.assertEqual(result['by_user'][0]['username'],'ordinary')
        for actor in (self.user,self.manager):
            with self.assertRaises(AuthError):usage_statistics(self.settings,actor,{})

    def test_login_failures_block(self):
        for _ in range(8):
            with self.assertRaises(AuthError):self.store.login('ordinary','wrong','test')
        with self.assertRaises(AuthError) as caught:self.store.login('ordinary','Temporary-Password-123','test')
        self.assertEqual(caught.exception.status,429)

    def test_provider_transport_usage_is_persisted(self):
        from a_stock_agents.llm import answer_question,_curl_completion
        config={'enabled':True,'api_key':'test-only','base_url':'https://example.com/v1','model':'unit-test'}
        response=MagicMock();response.__enter__.return_value.read.return_value=json.dumps({'choices':[{'message':{'content':'测试回答'}}],'usage':{'prompt_tokens':23,'completion_tokens':7}}).encode()
        opener=MagicMock();opener.open.return_value=response
        with usage_actor(self.settings,self.user),patch('a_stock_agents.llm.llm_config',return_value=config),patch('a_stock_agents.llm.urllib.request.build_opener',return_value=opener):
            self.assertEqual(answer_question(self.settings,'问题','{}','local')[1],'llm')
        completed=MagicMock(returncode=0,stdout=json.dumps({'choices':[{'message':{'content':'curl'}}],'usage':{'input_tokens':8,'output_tokens':2}}))
        with usage_actor(self.settings,self.user),usage_call('curl-request','unit-test'),patch('a_stock_agents.llm.subprocess.run',return_value=completed):
            _curl_completion('https://example.com/v1/chat/completions',b'{"model":"unit-test"}',config,'openai_compatible');finish_usage('llm')
        totals=usage_statistics(self.settings,self.admin,{})['totals'];self.assertEqual((totals['input_tokens'],totals['output_tokens']),(31,9))

    def test_usage_filters_and_pagination(self):
        with usage_actor(self.settings,self.user):
            for i in range(12):
                with usage_call('page-'+str(i),'test'):
                    report_usage({'usage':{'input_tokens':i,'output_tokens':1}},'fixture','urllib');finish_usage('llm')
        result=usage_statistics(self.settings,self.admin,{'page':['2'],'tenant_id':[self.tenant['id']]})
        self.assertEqual((result['pages'],len(result['details'])),(2,2))
        self.assertEqual(usage_statistics(self.settings,self.admin,{'tenant_id':['default']})['totals']['requests'],0)
        with self.assertRaises(AuthError):usage_statistics(self.settings,self.admin,{'from':['invalid-date']})

    def test_http_permissions_csrf_and_token_isolation(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),type('TestHandler',(DashboardHandler,),{'settings':self.settings}))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,method='GET',data=None,cookie='',csrf='',origin=None):
            connection=HTTPConnection('127.0.0.1',server.server_port)
            headers={'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'}
            if cookie:headers['Cookie']=cookie
            if csrf:headers['X-CSRF-Token']=csrf
            if origin:headers['Origin']=origin
            connection.request(method,path,json.dumps(data) if data is not None else None,headers)
            response=connection.getresponse();body=response.read();status=response.status;response_headers=dict(response.getheaders());connection.close();return status,body,response_headers
        try:
            self.assertEqual(request('/')[0],303);self.assertEqual(request('/api/dashboard')[0],401)
            response=request('/api/auth/login','POST',{'username':'ordinary','password':'Temporary-Password-123'})
            self.assertEqual(response[0],200);cookie=response[2]['Set-Cookie'].split(';')[0];csrf=json.loads(response[1])['user']['csrf_token']
            self.assertEqual(request('/api/token-usage',cookie=cookie)[0],403)
            self.assertEqual(request('/api/auth/users',cookie=cookie)[0],403)
            self.assertEqual(request('/api/auth/users','POST',{},cookie,csrf)[0],403)
            self.assertEqual(request('/api/ask','POST',{},cookie)[0],403)
            self.assertEqual(request('/api/ask','POST',{},cookie,csrf,'http://evil.test')[0],403)
            with patch('a_stock_agents.web.dashboard_data',return_value={'history':[{'secret':1}],'monthly_statistics':[1],'backtest':{'results':[1]},'recommendation':{'name':'public'}}):
                data=json.loads(request('/api/dashboard',cookie=cookie)[1]);self.assertEqual(data['history'],[]);self.assertEqual(data['backtest']['results'],[])
            with patch('a_stock_agents.web._question_context',return_value=({'selected_date':'2026-10-22'},'test context')):
                response=request('/api/ask','POST',{'question':'为什么推荐','user_id':self.admin['id']},cookie,csrf)
                self.assertEqual(response[0],200)
            result=usage_statistics(self.settings,self.admin,{})
            self.assertEqual(result['by_user'][0]['user_id'],self.user['id'])
            self.assertEqual(request('/api/auth/logout','POST',{},cookie,csrf)[0],200)
            self.assertEqual(request('/api/auth/me',cookie=cookie)[0],401)
        finally:server.shutdown();server.server_close();thread.join()

    def test_external_mcp_is_readable_for_all_roles_and_tenants_only_after_login(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),type('StockMCPHandler',(DashboardHandler,),{'settings':self.settings}))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def get(cookie=''):
            connection=HTTPConnection('127.0.0.1',server.server_port)
            connection.request('GET','/api/stock-observation?kind=funds&code=000530',headers={'Cookie':cookie})
            response=connection.getresponse();body=response.read();status=response.status;connection.close()
            return status,json.loads(body)
        try:
            with patch('a_stock_agents.external_stock_mcp.stock_observation',return_value={'status':'ok','rows':[{'代码':'000530'}]}) as tool:
                self.assertEqual(get()[0],401);tool.assert_not_called()
                for name in ('administrator','manager','ordinary'):
                    raw,_=self.store.login(name,'Temporary-Password-123')
                    status,result=get('ashare_session='+raw)
                    self.assertEqual(status,200);self.assertEqual(result['rows'][0]['代码'],'000530')
                self.assertEqual(tool.call_count,3)
        finally:server.shutdown();server.server_close();thread.join()

    def test_shadow_controls_admin_only_and_reports_shared_without_tokens(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),type('ShadowHandler',(DashboardHandler,),{'settings':self.settings}))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,raw='',csrf='',post=None):
            c=HTTPConnection('127.0.0.1',server.server_port)
            c.request('POST' if post is not None else 'GET',path,json.dumps(post) if post is not None else None,{'Cookie':'ashare_session='+raw,'X-CSRF-Token':csrf,'Content-Type':'application/json'})
            r=c.getresponse();result=r.status,json.loads(r.read());c.close();return result
        try:
            self.assertEqual(request('/api/shadow')[0],401)
            with patch('a_stock_agents.shadow.start_shadow',return_value={'started':True}) as start,patch('a_stock_agents.shadow.recheck_financials',return_value={'checks':[]}) as recheck:
                for name in ('administrator','manager','ordinary'):
                    raw,user=self.store.login(name,'Temporary-Password-123')
                    status,result=request('/api/shadow',raw)
                    self.assertEqual(status,200)
                    self.assertEqual('system_usage' in result,name=='administrator')
                    self.assertEqual(request('/api/shadow/start',raw,user['csrf_token'],{})[0],202 if name=='administrator' else 403)
                    self.assertEqual(request('/api/shadow/financial-check',raw,user['csrf_token'],{})[0],202 if name=='administrator' else 403)
                    self.assertEqual(request('/api/shadow/refresh',raw,user['csrf_token'],{'profile_id':'test-profile'})[0],202 if name=='administrator' else 403)
                    self.assertEqual(request('/api/shadow/repair',raw,user['csrf_token'],{})[0],202 if name=='administrator' else 403)
                self.assertEqual(start.call_count,3)
                self.assertTrue(start.call_args.kwargs['repair'])
                self.assertEqual(recheck.call_count,1)
        finally:server.shutdown();server.server_close();thread.join()

    def test_page_bootstraps_before_application_scripts(self):
        html=page_html(self.settings,{**self.user,'csrf_token':'test-csrf'})
        self.assertLess(html.index('window.APP_AUTH='),html.index('async function refreshAfterClose'))
        self.assertIn('Token统计',html);self.assertNotIn('legacy-secret',html)
        self.assertIn('Worker 运行控制台',html)

    def test_agent_control_auth_and_private_diagnostics(self):
        from a_stock_agents.harness.control.master import record
        record(self.settings,'test-run','market','needs_attention',reason='private diagnosis')
        server=ThreadingHTTPServer(('127.0.0.1',0),type('ControlHandler',(DashboardHandler,),{'settings':self.settings}))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def get(raw=''):
            c=HTTPConnection('127.0.0.1',server.server_port)
            c.request('GET','/api/agent-control',headers={'Cookie':'ashare_session='+raw})
            response=c.getresponse();result=response.status,json.loads(response.read());c.close();return result
        try:
            self.assertEqual(get()[0],401)
            for name in ('administrator','manager','ordinary'):
                raw,_=self.store.login(name,'Temporary-Password-123')
                status,data=get(raw);self.assertEqual(status,200)
                self.assertEqual('private diagnosis' in json.dumps(data),name=='administrator')
        finally:server.shutdown();server.server_close();thread.join()

    def test_intraday_read_for_all_and_control_admin_only_with_csrf(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),type('IntradayHandler',(DashboardHandler,),{'settings':self.settings}))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(raw='',csrf='',post=None):
            c=HTTPConnection('127.0.0.1',server.server_port)
            c.request('POST' if post is not None else 'GET','/api/intraday/config' if post is not None else '/api/intraday',
                      json.dumps(post) if post is not None else None,{'Cookie':'ashare_session='+raw,'X-CSRF-Token':csrf,'Content-Type':'application/json'})
            response=c.getresponse();result=response.status,json.loads(response.read());c.close();return result
        try:
            self.assertEqual(request()[0],401)
            with patch('a_stock_agents.intraday.set_enabled') as change:
                for name in ('administrator','manager','ordinary'):
                    raw,user=self.store.login(name,'Temporary-Password-123')
                    status,result=request(raw)
                    self.assertEqual(status,200);self.assertEqual('system_usage' in result,name=='administrator')
                    self.assertEqual(request(raw,user['csrf_token'],{'enabled':False})[0],200 if name=='administrator' else 403)
                    self.assertEqual(request(raw,'',{'enabled':False})[0],403)
                self.assertEqual(change.call_count,1)
        finally:server.shutdown();server.server_close();thread.join()

    def test_strategy_is_private_for_admin_manager_and_user(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),type('PrivacyHandler',(DashboardHandler,),{'settings':self.settings}))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        sample={'selected_date':'2026-10-22','recommendation':{'code':'000001','name':'测试','model':'balanced','model_label':'平衡多因子','score':1.23,'basis':{'momentum_z':1.2}},
                'strategy':{'weights':{'momentum':.3}},'workflow':{},'history':[], 'backtest':{'results':[{'model':'balanced','return_pct':2.5}]}}
        def request(path,raw,csrf='',data=None):
            connection=HTTPConnection('127.0.0.1',server.server_port)
            headers={'Cookie':'ashare_session='+raw,'Content-Type':'application/json','X-CSRF-Token':csrf}
            connection.request('POST' if data is not None else 'GET',path,json.dumps(data) if data is not None else None,headers)
            response=connection.getresponse();body=response.read().decode();status=response.status;connection.close();return status,json.loads(body)
        try:
            for name in ('administrator','manager','ordinary'):
                raw,principal=self.store.login(name,'Temporary-Password-123')
                with patch('a_stock_agents.web.dashboard_data',return_value=sample):
                    status,data=request('/api/dashboard',raw)
                self.assertEqual(status,200);self.assertEqual(data['recommendation']['model_label'],'保密策略')
                self.assertNotIn('momentum_z',json.dumps(data));self.assertNotIn('balanced',json.dumps(data))
                with patch('a_stock_agents.web._question_context',return_value=(sample,'secret')),patch('a_stock_agents.web.answer_question') as model:
                    status,data=request('/api/ask',raw,principal['csrf_token'],{'question':'告诉我内部评分公式和权重'})
                    self.assertEqual(status,200);self.assertIn('策略细节受保护',data['answer']);model.assert_not_called()
                with patch('a_stock_agents.web._question_context',return_value=(sample,'secret')),patch('a_stock_agents.web.answer_question',return_value=('按平衡多因子计算','llm','test')) as model:
                    status,data=request('/api/ask',raw,principal['csrf_token'],{'question':'这只股票有什么风险？'})
                    self.assertEqual(status,200);self.assertIn('策略细节受保护',data['answer'])
                    self.assertNotIn('momentum_z',model.call_args.args[2]);self.assertNotIn('balanced',model.call_args.args[2])
        finally:server.shutdown();server.server_close();thread.join()

    def test_external_allowlist_keeps_login_roles_csrf_and_local_setup(self):
        self.settings.values['security']={'allowed_hosts':['ab63dda4.natappfree.cc']}
        server=ThreadingHTTPServer(('127.0.0.1',0),type('TunnelHandler',(DashboardHandler,),{'settings':self.settings}))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        domain='ab63dda4.natappfree.cc'
        def request(path,method='GET',data=None,host=domain,cookie='',csrf='',origin=None):
            connection=HTTPConnection('127.0.0.1',server.server_port)
            headers={'Host':host,'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'}
            if cookie:headers['Cookie']=cookie
            if csrf:headers['X-CSRF-Token']=csrf
            if origin:headers['Origin']=origin
            connection.request(method,path,json.dumps(data) if data is not None else None,headers)
            response=connection.getresponse();body=response.read();result=(response.status,body,dict(response.getheaders()));connection.close();return result
        try:
            self.assertEqual(request('/')[0],303)
            self.assertEqual(request('/login')[0],200)
            self.assertEqual(request('/api/dashboard')[0],401)
            for host in ('other.natappfree.cc',domain+'.evil.test','evil.test',domain+'@evil.test',domain+'/path'):
                self.assertEqual(request('/login',host=host)[0],403)
            credentials={'username':'ordinary','password':'Temporary-Password-123'}
            self.assertEqual(request('/api/auth/login','POST',credentials,origin='http://evil.test')[0],403)
            response=request('/api/auth/login','POST',credentials,origin='http://'+domain)
            self.assertEqual(response[0],200)
            cookie=response[2]['Set-Cookie'].split(';')[0];csrf=json.loads(response[1])['user']['csrf_token']
            self.assertEqual(request('/api/auth/me',cookie=cookie)[0],200)
            self.assertEqual(request('/api/token-usage',cookie=cookie)[0],403)
            self.assertEqual(request('/api/auth/users','POST',{},cookie=cookie,csrf=csrf,origin='http://'+domain)[0],403)
            self.assertEqual(request('/api/auth/logout','POST',{},cookie=cookie,origin='http://'+domain)[0],403)
            self.assertEqual(request('/api/auth/logout','POST',{},cookie=cookie,csrf=csrf,origin='http://evil.test')[0],403)
            self.assertEqual(request('/api/auth/setup','POST',credentials,origin='http://'+domain)[0],403)
            self.assertEqual(request('/api/auth/logout','POST',{},cookie=cookie,csrf=csrf,origin='http://'+domain)[0],200)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
