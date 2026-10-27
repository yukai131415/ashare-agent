"""Bounded retries and same-endpoint transport fallback for public financial data."""
import json
import subprocess
import time
import urllib.error
from urllib.parse import urlencode

from .console_log import console_event
from .errors import SourceBlockedError


class FinancialUnavailable(SourceBlockedError):
    def __init__(self,category,attempts):
        self.category,self.attempts=category,attempts
        super().__init__('财务数据暂不可用：'+category+'；仅代表本次指定股票查询失败，不代表其他股票没有财报')


def classify(exc):
    current=exc
    while current is not None:
        if isinstance(current,urllib.error.HTTPError):
            return ('rate_limited' if current.code==429 else 'access_denied' if current.code in (401,403) else 'upstream_http'),current.code
        current=current.__cause__
    text=str(exc).lower()
    if 'timed out' in text or isinstance(exc,TimeoutError):return 'timeout',None
    if 'ssl' in text or 'tls' in text:return 'tls',None
    if isinstance(exc,(json.JSONDecodeError,UnicodeDecodeError)):return 'invalid_response',None
    return 'connection',None


def fetch_financial_json(fetch,url,params):
    attempts=[]
    for index in range(3):
        if index:
            from .harness.runtime import active_harness
            from .harness.policy import policy
            runtime=active_harness()
            if runtime and not runtime.store.reserve_recovery(runtime.run_id,policy(runtime.settings).max_run_recoveries):raise FinancialUnavailable('retry_budget_exhausted',attempts)
        transport='urllib' if index<2 else 'curl'
        started=time.monotonic()
        try:
            console_event('financial_fetch_request',endpoint=url,parameters=params,transport=transport,attempt=index+1)
            if transport=='urllib':payload=fetch()
            else:
                completed=subprocess.run(['curl.exe','--silent','--show-error','--connect-timeout','5','--max-time','12',
                    '--write-out','\n%{http_code}',url+'?'+urlencode(params)],capture_output=True,timeout=15)
                raw,_,status=completed.stdout.rpartition(b'\n')
                try:status=int(status)
                except ValueError:status=0
                if status>=400:raise urllib.error.HTTPError(url,status,'financial endpoint error',None,None)
                if status!=200 or completed.returncode not in (0,56):raise OSError('备用传输未获得完整成功响应')
                payload=json.loads(raw.decode('utf-8'))
            if not isinstance(payload,dict):raise json.JSONDecodeError('财务响应不是JSON对象','',0)
            attempts.append({'transport':transport,'ok':True,'duration_ms':round((time.monotonic()-started)*1000)})
            console_event('financial_fetch_finished',ok=True,response=payload,attempts=attempts)
            return payload,attempts
        except (SourceBlockedError,urllib.error.URLError,TimeoutError,OSError,json.JSONDecodeError,UnicodeDecodeError,subprocess.SubprocessError) as exc:
            category,status=classify(exc)
            attempts.append({'transport':transport,'ok':False,'category':category,'http_status':status,
                             'cause_type':type(exc.__cause__ or exc).__name__,'duration_ms':round((time.monotonic()-started)*1000)})
            console_event('financial_fetch_attempt',**attempts[-1])
            # Do not switch transports to bypass authorization, blocks or rate limits.
            if category in ('access_denied','rate_limited'):raise FinancialUnavailable(category,attempts) from exc
            if index==2:raise FinancialUnavailable(category,attempts) from exc
            time.sleep(.4)
