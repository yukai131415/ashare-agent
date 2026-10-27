"""HTTP access control shared by all dashboard entry points."""
import hmac
import json
import time
import threading
from http.cookies import SimpleCookie
from urllib.parse import urlparse,parse_qs

from .auth import auth_store,tenant_settings,AuthError
from .token_usage import usage_actor,usage_statistics
from .console_log import log_scope,console_event

COOKIE='ashare_session'
_client_events={}
_client_events_lock=threading.Lock()


from .login_view import login_page


def bootstrap(user):
    identity=json.dumps(user,ensure_ascii=False).replace('<','\\u003c')
    from pathlib import Path
    tracing=Path(__file__).with_name('static').joinpath('request-tracing.js').read_text(encoding='utf-8')
    return '<script>window.APP_AUTH='+identity+''';(()=>{const original=window.fetch;window.fetch=async(input,options={})=>{const url=new URL(typeof input==='string'?input:input.url,location.href);if(url.origin===location.origin){options={...options,headers:new Headers(options.headers)};if(!['GET','HEAD'].includes((options.method||(input instanceof Request?input.method:'GET')).toUpperCase()))options.headers.set('X-CSRF-Token',APP_AUTH.csrf_token);}const response=await original(input,options);if(response.status===401&&url.origin===location.origin)location.replace('/login');return response;};})();</script>'''+ '<script>'+tracing+'</script>'


class AccessControl:
    def _json(self,value,status=200):self._send(json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8'),'application/json; charset=utf-8',status)

    def _check_host(self):
        headers=self.headers.get_all('Host',[])
        if len(headers)!=1:raise AuthError('无效的访问主机。',403)
        host=headers[0]
        try:
            parsed=urlparse('//'+host)
            hostname=parsed.hostname
            parsed.port  # Reject malformed ports instead of trusting the hostname alone.
        except ValueError as exc:raise AuthError('无效的访问主机。',403) from exc
        if not hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or any(c.isspace() for c in host):raise AuthError('无效的访问主机。',403)
        allowed={'localhost','127.0.0.1','::1',self.server.server_address[0]}
        configured=self.settings.values.get('security',{}).get('allowed_hosts',[])
        if isinstance(configured,list):
            # Exact names only: never allow *.natappfree.cc or an arbitrary suffix.
            allowed.update(value.lower() for value in configured if isinstance(value,str) and value and not any(c in value for c in '* /\\@:?#'))
        if hostname not in allowed:raise AuthError('不允许的访问主机。',403)
        return host,hostname

    def _check_origin(self,host,allow_missing=False):
        origin=self.headers.get('Origin')
        if origin in {'http://'+host,'https://'+host}:return
        if origin is None and allow_missing:return
        raise AuthError('跨站请求已拒绝。',403)

    def _cookie(self):
        try:
            jar=SimpleCookie();jar.load(self.headers.get('Cookie',''));return jar[COOKIE].value if COOKIE in jar else ''
        except Exception:return ''

    def _guard(self,post=False):
        host,_=self._check_host()
        if post:self._check_origin(host,allow_missing=True)
        self.principal=auth_store(self.settings).session(self._cookie())
        if not self.principal:raise AuthError('请先登录。',401)
        if post and not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),self.principal['csrf_token']):raise AuthError('请求校验失败，请刷新页面。',403)

    def do_GET(self):
        path=urlparse(self.path).path
        try:
            self._check_host()
            store=auth_store(self.settings)
            if path in ('/login','/setup'):
                setup=not store.initialized()
                self._send(login_page(setup).encode('utf-8'),'text/html; charset=utf-8');return
            try:self._guard()
            except AuthError as exc:
                if exc.status==401 and path in ('/','/index.html'):
                    self.send_response(303);self.send_header('Location','/login' if store.initialized() else '/setup');self.send_header('Content-Length','0');self.end_headers();return
                raise
            if path=='/api/auth/me':self._json(self.principal);return
            if path=='/api/auth/users':self._json({'users':store.users(self.principal)});return
            if path=='/api/auth/tenants':self._json({'tenants':store.tenants(self.principal)});return
            if path=='/api/token-usage':self._json(usage_statistics(self.settings,self.principal,parse_qs(urlparse(self.path).query)));return
            if path=='/api/llm-config':
                from .llm import public_config
                result=public_config(tenant_settings(self.settings,self.principal['tenant_id']))
                if self.principal['role']=='user':result={k:result.get(k) for k in ('enabled','model','api_key_configured')}
                self._json(result);return
            self._dashboard_GET()
        except AuthError as exc:self._json({'error':str(exc)},exc.status)

    def do_POST(self):
        parsed=urlparse(self.path);path=parsed.path
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<0 or length>100_000:raise AuthError('请求大小无效。',413)
            if path in ('/api/auth/setup','/api/auth/login'):
                host,hostname=self._check_host()
                self._check_origin(host,allow_missing=self.headers.get('X-Requested-With')=='XMLHttpRequest')
                values=json.loads(self.rfile.read(length).decode('utf-8'))
                if not isinstance(values,dict):raise AuthError('需要JSON对象。')
                console_event('http_request_body',request=values)
                store=auth_store(self.settings)
                if path.endswith('/setup'):
                    if self.client_address[0] not in ('127.0.0.1','::1') or hostname not in ('localhost','127.0.0.1','::1') or any(self.headers.get(k) for k in ('Forwarded','X-Forwarded-For','X-Forwarded-Host','X-Forwarded-Proto')):raise AuthError('首次设置只能在本机完成。',403)
                    store.setup(values.get('username'),values.get('password'),values.get('display_name',''),values.get('tenant_name','默认租户'))
                raw,user=store.login(values.get('username'),values.get('password'),self.client_address[0])
                self._session_cookie=f'{COOKIE}={raw}; Path=/; HttpOnly; SameSite=Lax; Max-Age=28800'
                self._json({'ok':True,'user':user});return
            self._guard(post=True)
            if path=='/api/client-events':
                values=json.loads(self.rfile.read(length).decode('utf-8'))
                if not isinstance(values,dict) or values.get('event') not in ('fetch_failed','javascript_error','unhandled_rejection'):raise AuthError('日志事件无效。',400)
                with _client_events_lock:
                    now=time.monotonic();key=self.principal['id'];stamp,count=_client_events.get(key,(now,0))
                    if now-stamp>=60:stamp,count=now,0
                    if count>=30:raise AuthError('日志上报过于频繁。',429)
                    if len(_client_events)>1000:_client_events.clear()
                    _client_events[key]=(stamp,count+1)
                import re
                safe_path=str(values.get('path','')).split('?')[0].split('#')[0][:200]
                related=str(values.get('related_request_id',''))
                console_event('frontend_event',client_event=values['event'],page_path=safe_path if re.fullmatch(r'/[A-Za-z0-9_./-]*',safe_path) else '[invalid path]',
                              related_request_id=related if re.fullmatch(r'[A-Za-z0-9-]{8,64}',related) else None,
                              duration_ms=min(3600000,max(0,int(values.get('duration_ms',0)))),line=min(1000000,max(0,int(values.get('line',0)))),
                              user_id=self.principal['id'],tenant_id=self.principal['tenant_id'])
                self._json({'ok':True},202);return
            if self.principal['role']=='user' and path not in ('/api/ask','/api/auth/logout','/api/research-search'):raise AuthError('普通用户不能执行此操作。',403)
            if path.startswith('/api/auth/'):
                values=json.loads(self.rfile.read(length).decode('utf-8'))
                if not isinstance(values,dict):raise AuthError('需要JSON对象。')
                console_event('http_request_body',request=values)
                store=auth_store(self.settings)
                if path=='/api/auth/logout':
                    store.logout(self._cookie());self._session_cookie=f'{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0';self._json({'ok':True});return
                if path=='/api/auth/users':self._json(store.create_user(self.principal,values),201);return
                if path=='/api/auth/tenants':self._json(store.create_tenant(self.principal,values.get('name')),201);return
                if path=='/api/auth/users/active':
                    if not isinstance(values.get('active'),bool):raise AuthError('状态必须为布尔值。')
                    self._json(store.set_active(self.principal,values.get('user_id'),values['active']));return
                raise AuthError('接口不存在。',404)
            with usage_actor(self.settings,self.principal,'connection_test' if path=='/api/llm-test' else 'ask'),log_scope(tenant_id=self.principal['tenant_id'],user_id=self.principal['id'],username=self.principal['username']):self._dashboard_POST()
        except AuthError as exc:self._json({'error':str(exc)},exc.status)
        except (ValueError,UnicodeDecodeError):self._json({'error':'无效请求。'},400)

    def _send(self,body,content_type,status=200):
        if getattr(self,'principal',None) and content_type.startswith('application/json') and status==200:
            from .strategy_privacy import public_dashboard,public_research
            path=urlparse(self.path).path
            if path in ('/api/dashboard','/api/research-dashboard'):
                project=public_dashboard if path=='/api/dashboard' else public_research
                body=json.dumps(project(json.loads(body)),ensure_ascii=False,allow_nan=False).encode('utf-8')
        if getattr(self,'principal',None) and self.principal['role']=='user' and content_type.startswith('application/json') and status==200:
            path=urlparse(self.path).path
            if path=='/api/dashboard':
                value=json.loads(body);value['history']=[];value['monthly_statistics']=[]
                if isinstance(value.get('backtest'),dict):value['backtest']['results']=[]
                body=json.dumps(value,ensure_ascii=False).encode('utf-8')
            elif path=='/api/research-dashboard':
                value=json.loads(body)
                value.update(candidates=[],ranking={'full_snapshot':False,'message':'此账号仅可访问监控总览与每日简报。'},performance={'summary':None,'trades':[],'curve':[],'notes':[]},sectors={'rows':[]},industry={},sync={'status':'restricted','running':False})
                body=json.dumps(value,ensure_ascii=False).encode('utf-8')
        if content_type.startswith('application/json'):
            from .http_logging import QUIET_STATUS_PATHS
            quiet=self.command=='GET' and urlparse(self.path).path in QUIET_STATUS_PATHS and status<400
            try:response_data=json.loads(body)
            except (ValueError,UnicodeDecodeError):response_data='[响应不是有效JSON]'
            console_event('http_response',_console=not quiet,status=status,response_bytes=len(body),response=response_data)
        self.send_response(status);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY');self.send_header('Referrer-Policy','same-origin')
        if getattr(self,'_session_cookie',None):self.send_header('Set-Cookie',self._session_cookie)
        self.end_headers();self.wfile.write(body)
