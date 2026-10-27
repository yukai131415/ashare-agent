"""Disposable UI fixture, not imported by the application. No production data or keys.

Run manually with PYTHONPATH=src. /preview/{role} issues a temporary test session.
"""
import secrets
import tempfile
from pathlib import Path
from http.server import ThreadingHTTPServer
from a_stock_agents.config import Settings,load_settings
from a_stock_agents.auth import auth_store
from a_stock_agents.storage import ensure_runtime
from a_stock_agents.token_usage import usage_actor,usage_call,report_usage,finish_usage
from a_stock_agents.web import DashboardHandler


def main():
    with tempfile.TemporaryDirectory(prefix='ashare-auth-ui-') as folder:
        settings=Settings(Path(folder),load_settings().values);ensure_runtime(settings.runtime)
        settings.values['llm']={'enabled':False,'api_key_env':'UNUSED_TEST_KEY'}
        store=auth_store(settings);password=secrets.token_urlsafe(24)
        admin=store.setup('preview_admin',password,'验收管理员','隔离测试租户')
        for role in ('manager','user'):store.create_user(admin,{'username':'preview_'+role,'password':password,'role':role,'display_name':'验收'+role})
        with usage_actor(settings,admin):
            with usage_call('fixture-request','示例模型（测试数据）'):
                report_usage({'usage':{'input_tokens':1234,'output_tokens':567}},'fixture','fixture');finish_usage('llm')
        class Preview(DashboardHandler):
            def do_GET(self):
                if self.path in ('/preview/admin','/preview/manager','/preview/user'):
                    role=self.path.rsplit('/',1)[1];raw,_=store.login('preview_'+role,password)
                    self.send_response(303);self.send_header('Location','/');self.send_header('Set-Cookie','ashare_session='+raw+'; Path=/; HttpOnly; SameSite=Lax');self.send_header('Content-Length','0');self.end_headers();return
                super().do_GET()
        Preview.settings=settings
        server=ThreadingHTTPServer(('127.0.0.1',0),Preview)
        print('ISOLATED_UI_PREVIEW http://127.0.0.1:'+str(server.server_port),flush=True)
        try:server.serve_forever()
        finally:server.server_close()


if __name__=='__main__':main()
