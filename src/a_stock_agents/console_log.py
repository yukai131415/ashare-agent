"""Log4j-style readable output plus structured JSON archive, with redaction."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone, timedelta
import json
import re
import threading
import logging
import os
from logging.handlers import RotatingFileHandler

_scope = ContextVar('console_log_scope', default={})
_secrets = ContextVar('console_log_secrets', default=())
_console_stream=ContextVar('console_log_stream',default=None)
_lock = threading.Lock()
_file_handler = None
_secret_keys = {'apikey', 'key', 'secretkey', 'clientsecret', 'credential', 'authorization', 'password', 'passwordhash', 'currentpassword', 'newpassword', 'oldpassword', 'secret', 'accesstoken', 'refreshtoken', 'cookie', 'setcookie', 'csrftoken', 'xcsrftoken', 'token', 'session', 'sessionid'}


def event_level(entry):
    explicit=str(entry.get('level','')).upper()
    if explicit in ('DEBUG','INFO','WARN','ERROR'):return explicit
    event=str(entry.get('event',''));status=entry.get('status')
    if (isinstance(status,int) and status>=500) or entry.get('ok') is False or any(word in event for word in ('failed','exception','error')) or status in ('failed','needs_attention','failed_attempt'):return 'ERROR'
    if (isinstance(status,int) and status>=400) or status in ('retry_wait','diagnosing','unconfirmed') or any(word in event for word in ('retry','fallback','disconnected')):return 'WARN'
    return 'INFO'


def format_log_line(entry):
    """Pattern: yyyy-MM-dd HH:mm:ss.SSS LEVEL [thread] logger [MDC] - message."""
    stamp=datetime.fromisoformat(entry['at']).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    def clean(value):return str(value).replace('\r','\\r').replace('\n','\\n').replace('\t','\\t')
    module=clean(entry.get('component') or entry.get('agent') or 'app')
    context=' '.join(f'{key}={clean(entry[key])}' for key in ('pid','request_id','http_request_id','call_id','run_id','worker') if entry.get(key) is not None)
    fields=[]
    omitted={'at','level','thread','component','pid','request_id','http_request_id','call_id','run_id','worker','event'}
    for key,value in entry.items():
        if key in omitted or value is None:continue
        text=json.dumps(value,ensure_ascii=False,default=str) if isinstance(value,(dict,list,tuple)) else str(value)
        limit=8000 if key in ('request','response','arguments','result','traceback') else 2000
        text=clean(text)
        if len(text)>limit:text=text[:limit]+f'…[已截断，原长{len(text)}字符；完整事件见JSON日志]'
        fields.append(f'{clean(key)}={text}')
    return f"{stamp} {event_level(entry):<5} [{clean(entry.get('thread','main'))}] astock.{module} [{context}] - {clean(entry['event'])}"+(' | '+' '.join(fields) if fields else '')


class ArchiveHandler(RotatingFileHandler):
    def __init__(self,path):
        super().__init__(path,maxBytes=10*1024*1024,backupCount=5,encoding='utf-8')
        self.readable=RotatingFileHandler(path.with_suffix('.log'),maxBytes=10*1024*1024,backupCount=5,encoding='utf-8')
        self.readable.setFormatter(logging.Formatter('%(message)s'))

    def emit(self,record):
        super().emit(record)
        entry=getattr(record,'entry',None)
        if entry is not None:
            message=format_log_line(entry)
            if entry.get('traceback'):message+='\n'+str(entry['traceback'])
            self.readable.emit(logging.LogRecord('astock',logging.INFO,'',0,message,(),None))

    def close(self):
        if hasattr(self,'readable'):self.readable.close()
        super().close()


class StandardLoggingBridge(logging.Handler):
    def emit(self,record):
        fields={'component':record.name,'level':'WARN' if record.levelname=='WARNING' else record.levelname,'message':record.getMessage()}
        if record.exc_info:fields['traceback']=logging.Formatter().formatException(record.exc_info)
        console_event('python_log',**fields)


def configure_console_file(path):
    """Keep bounded local logs even when stdout is detached/redirected."""
    global _file_handler
    path.parent.mkdir(parents=True,exist_ok=True)
    with _lock:
        if _file_handler:_file_handler.close()
        _file_handler=ArchiveHandler(path)
        _file_handler.setFormatter(logging.Formatter('%(message)s'))
    for name in ('a_stock_agents','py.warnings'):
        logger=logging.getLogger(name)
        if not any(isinstance(handler,StandardLoggingBridge) for handler in logger.handlers):logger.addHandler(StandardLoggingBridge())
        logger.setLevel(logging.INFO);logger.propagate=False
    logging.captureWarnings(True)


def sanitize(value):
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if re.sub(r'[^a-z]', '', str(k).lower()) in _secret_keys else sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        for secret in _secrets.get():
            if secret:
                value = value.replace(secret, '[REDACTED]')
        value = re.sub(r'(?i)Bearer\s+[^\s"\']+', 'Bearer [REDACTED]', value)
        value = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[REDACTED]', value)
        value = re.sub(r'(?i)(https?://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', value)
        value = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*)[^\s&"\',}]+', r'\1[REDACTED]', value)
        value = re.sub(r'''(?i)(["'](?:api[_-]?key|authorization|(?:current|new|old)?password|secret|(?:access|refresh)[_-]?token|csrf[_-]?token|cookie|token|session)["']\s*:\s*)["'][^"']*["']''',r'\1"[REDACTED]"',value)
    return value


@contextmanager
def console_output(stream):
    """MCP stdio reserves stdout for JSON-RPC; diagnostics must use stderr."""
    token=_console_stream.set(stream)
    try:yield
    finally:_console_stream.reset(token)


@contextmanager
def log_scope(*, secrets=(), **fields):
    token = _scope.set({**_scope.get(), **fields})
    secret_token = _secrets.set((*_secrets.get(), *secrets))
    try:
        yield
    finally:
        _secrets.reset(secret_token)
        _scope.reset(token)


@contextmanager
def external_operation(operation,**fields):
    """Reusable boundary logging for external calls that bypass HttpClient."""
    import time
    import traceback
    import uuid
    started=time.monotonic();result={}
    with log_scope(component=operation,call_id=uuid.uuid4().hex):
        console_event('external_request',**fields)
        try:
            yield result
        except Exception as exc:
            console_event('external_failed',error_type=type(exc).__name__,message=str(exc),traceback=traceback.format_exc(),duration_ms=round((time.monotonic()-started)*1000))
            raise
        else:console_event('external_response',**result,duration_ms=round((time.monotonic()-started)*1000))


def console_event(event, *, _console=True, **fields):
    entry = sanitize({'at': datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='milliseconds'), 'pid':os.getpid(), 'thread':threading.current_thread().name, **_scope.get(), 'event': event, **fields})
    entry['level']=event_level(entry)
    line = json.dumps(entry, ensure_ascii=False, default=str)
    with _lock:
        if _console:
            try:
                print(format_log_line(entry),file=_console_stream.get(),flush=True)
            except (OSError, UnicodeError):pass
        if _file_handler:
            try:
                record=logging.LogRecord('astock',logging.INFO,'',0,line,(),None);record.entry=entry
                _file_handler.emit(record)
            except (OSError,UnicodeError):pass
    return entry
