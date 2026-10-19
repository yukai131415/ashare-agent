"""Manage the project's Windows-compatible Milvus Lite server on loopback only."""
from __future__ import annotations
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from urllib.parse import urlsplit
from .contracts import iso_now
from .storage import atomic_write_json


def reachable(host,port):
    try:
        with socket.create_connection((host,port),timeout=0.3):return True
    except OSError:return False


@contextmanager
def startup_lock(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as handle:
        if handle.tell()==0:handle.write(b'0');handle.flush()
        handle.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(handle.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(handle,fcntl.LOCK_EX)
        try:yield
        finally:
            handle.seek(0)
            if os.name=='nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(handle,fcntl.LOCK_UN)


def ensure_local_milvus(settings):
    config=settings.values.get('research',{})
    if not config.get('enabled') or not config.get('auto_start',False):return {'status':'not_managed'}
    uri=os.getenv('ASTOCK_MILVUS_URI') or config.get('uri','http://127.0.0.1:19530')
    parsed=urlsplit(uri)
    if parsed.hostname not in ('127.0.0.1','localhost','::1'):return {'status':'external'}
    port,host=parsed.port or 19530,parsed.hostname
    if reachable(host,port):return {'status':'running','uri':uri}
    with startup_lock(settings.runtime/'state/milvus_start.lock'):
        if reachable(host,port):return {'status':'running','uri':uri}
        logs=settings.runtime/'logs';logs.mkdir(parents=True,exist_ok=True)
        with (logs/'milvus.out.log').open('ab') as stdout,(logs/'milvus.err.log').open('ab') as stderr:
            # FAISS' narrow Windows file API cannot reopen indexes at Chinese absolute
            # paths. Keep an ASCII relative data path under the explicit project cwd.
            child=subprocess.Popen([sys.executable,'-u','-m','milvus_lite.cmdline','server','--data-dir',os.path.join('runtime','milvus'),
                '--host',host,'--port',str(port)],cwd=settings.root,stdout=stdout,stderr=stderr,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            if child.poll() is not None:raise RuntimeError('Milvus Lite 启动失败，请查看 runtime/logs/milvus.err.log')
            if reachable(host,port):
                state={'status':'running','pid':child.pid,'uri':uri,'started_at':iso_now(),'deployment':'milvus-lite'}
                atomic_write_json(settings.runtime/'state/milvus_service.json',state)
                return state
            time.sleep(0.2)
        raise TimeoutError('Milvus Lite 启动超过20秒，请查看服务日志')
