"""Request lifecycle logging without credentials, bodies, or response payloads."""
import re
import time
import traceback
import uuid
from urllib.parse import urlparse,parse_qs
from .console_log import console_event,log_scope

# Successful read-only status polling stays in the file, not the IDE console.
QUIET_STATUS_PATHS=frozenset({'/api/dashboard','/api/research-dashboard','/api/research-status','/api/shadow','/api/intraday','/api/agent-control'})


class HTTPLogging:
    def do_GET(self):self._logged_request(super().do_GET)
    def do_POST(self):self._logged_request(super().do_POST)

    def _logged_request(self,dispatch):
        supplied=self.headers.get('X-Request-ID','')
        self.request_id=supplied if re.fullmatch(r'[A-Za-z0-9-]{8,64}',supplied) else uuid.uuid4().hex
        self._log_status=None;self._log_headers_sent=False;self.principal=None
        started=time.monotonic();parsed=urlparse(self.path)
        quiet=self.command=='GET' and parsed.path in QUIET_STATUS_PATHS
        query={k:v for k,v in parse_qs(parsed.query).items() if k in ('date','kind','code','page','limit')}
        with log_scope(request_id=self.request_id,http_request_id=self.request_id,component='http',method=self.command,path=parsed.path):
            console_event('http_request_started',_console=not quiet,query=query,client_ip=self.client_address[0],content_length=self.headers.get('Content-Length','0'))
            try:dispatch()
            except (BrokenPipeError,ConnectionResetError):
                console_event('http_client_disconnected')
            except Exception as exc:
                console_event('http_request_exception',error_type=type(exc).__name__,traceback=traceback.format_exc())
                if not self._log_headers_sent:
                    self._json({'error':'服务处理异常，请使用请求编号联系管理员。','request_id':self.request_id},500)
            finally:
                user=getattr(self,'principal',None) or {}
                console_event('http_request_finished',_console=not quiet or not self._log_status or self._log_status>=400,status=self._log_status or 0,duration_ms=round((time.monotonic()-started)*1000),
                              user_id=user.get('id'),tenant_id=user.get('tenant_id'),role=user.get('role'))

    def send_response(self,code,message=None):
        self._log_status=code
        super().send_response(code,message)
        if getattr(self,'request_id',None):self.send_header('X-Request-ID',self.request_id)

    def end_headers(self):
        super().end_headers();self._log_headers_sent=True
