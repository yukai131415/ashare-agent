"""Persist provider-reported usage with server-authenticated tenant/user identity."""
from contextlib import contextmanager, closing
from contextvars import ContextVar
from datetime import date

from .auth import auth_store,AuthError,now_text

_actor=ContextVar('usage_actor',default=None)
_call=ContextVar('usage_call',default=None)


@contextmanager
def usage_actor(settings,principal,purpose='ask'):
    token=_actor.set((auth_store(settings),principal,purpose))
    try:yield
    finally:_actor.reset(token)


@contextmanager
def usage_call(request_id,model):
    actor=_actor.get()
    token=_call.set((actor,request_id) if actor else None)
    try:
        if actor:
            store,user,purpose=actor
            with store.connection() as db:
                db.execute('INSERT INTO token_requests(id,tenant_id,user_id,model,purpose,started_at) VALUES(?,?,?,?,?,?)',
                           (request_id,user['tenant_id'],user['id'],str(model),purpose,now_text()))
        yield
    except Exception:
        finish_usage('error')
        raise
    finally:
        if actor:
            with actor[0].connection() as db:db.execute("UPDATE token_requests SET status=CASE WHEN status='running' THEN 'error' ELSE status END,finished_at=COALESCE(finished_at,?) WHERE id=?",(now_text(),request_id))
        _call.reset(token)


def finish_usage(status):
    call=_call.get()
    if call:
        (store,_,_),request_id=call
        with store.connection() as db:db.execute('UPDATE token_requests SET status=?,finished_at=? WHERE id=?',(status,now_text(),request_id))


def _count(value):
    return value if isinstance(value,int) and not isinstance(value,bool) and value>=0 else None


def report_usage(response,provider,transport):
    call=_call.get()
    if not call:return
    usage=response.get('usage') or {}
    if not isinstance(usage,dict):usage={}
    sent=_count(usage.get('prompt_tokens',usage.get('input_tokens')))
    received=_count(usage.get('completion_tokens',usage.get('output_tokens')))
    total=_count(usage.get('total_tokens'))
    if total is None and sent is not None and received is not None:total=sent+received
    (store,_,_),request_id=call
    with store.connection() as db:
        db.execute('INSERT OR IGNORE INTO token_attempts(request_id,transport,provider,input_tokens,output_tokens,total_tokens,provider_request_id,recorded_at) VALUES(?,?,?,?,?,?,?,?)',
                   (request_id,transport,provider,sent,received,total,str(response.get('id') or response.get('request_id') or '')[:200],now_text()))


def usage_statistics(settings,principal,params):
    if principal['role']!='admin':raise AuthError('仅管理员可查看Token统计。',403)
    clauses=[];args=[]
    for key,column in [('tenant_id','r.tenant_id'),('user_id','r.user_id')]:
        value=params.get(key,[''])[0]
        if value:clauses.append(column+'=?');args.append(value)
    for key,op in [('from','>='),('to','<=')]:
        value=params.get(key,[''])[0]
        if value:
            try:date.fromisoformat(value)
            except ValueError as exc:raise AuthError('无效统计日期。') from exc
            clauses.append(f'substr(r.started_at,1,10){op}?');args.append(value)
    try:page=max(1,int(params.get('page',['1'])[0]))
    except (ValueError,TypeError) as exc:raise AuthError('无效页码。') from exc
    where='WHERE '+' AND '.join(clauses) if clauses else ''
    store=auth_store(settings)
    base=f'''FROM token_requests r JOIN users u ON u.id=r.user_id JOIN tenants t ON t.id=r.tenant_id
             LEFT JOIN (SELECT request_id,SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,
                COUNT(*) attempts,SUM(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL THEN 1 ELSE 0 END) unreported
                FROM token_attempts GROUP BY request_id) a ON a.request_id=r.id {where}'''
    with store.connection() as db:
        totals=dict(db.execute(f'''SELECT COUNT(*) requests,COALESCE(SUM(a.attempts),0) provider_responses,
              COALESCE(SUM(a.input_tokens),0) input_tokens,COALESCE(SUM(a.output_tokens),0) output_tokens,
              COALESCE(SUM(a.unreported),0) unreported_usage,SUM(CASE WHEN r.status='local' THEN 1 ELSE 0 END) local_requests,
              SUM(CASE WHEN r.status IN ('fallback','error') THEN 1 ELSE 0 END) failed_requests {base}''',args).fetchone())
        by_user=[dict(row) for row in db.execute(f'''SELECT r.tenant_id,t.name tenant_name,r.user_id,u.username,u.display_name,
                COUNT(*) requests,SUM(a.input_tokens) input_tokens,SUM(a.output_tokens) output_tokens,
                COALESCE(SUM(a.unreported),0) unreported_usage {base} GROUP BY r.tenant_id,r.user_id ORDER BY COALESCE(SUM(a.input_tokens),0)+COALESCE(SUM(a.output_tokens),0) DESC''',args)]
        by_day=[dict(row) for row in db.execute(f'''SELECT substr(r.started_at,1,10) day,COUNT(*) requests,
                 SUM(a.input_tokens) input_tokens,SUM(a.output_tokens) output_tokens {base} GROUP BY day ORDER BY day DESC''',args)]
        pages=max(1,(totals['requests']+9)//10);page=min(page,pages)
        details=[dict(row) for row in db.execute(f'''SELECT r.id,r.started_at,r.finished_at,r.model,r.purpose,r.status,r.tenant_id,t.name tenant_name,
                    u.username,u.display_name,a.input_tokens,a.output_tokens,COALESCE(a.attempts,0) provider_responses,
                    COALESCE(a.unreported,0) unreported_usage {base} ORDER BY r.started_at DESC,r.id DESC LIMIT 10 OFFSET ?''',(*args,(page-1)*10))]
    return {'shadow_usage':shadow_usage_statistics(settings,params),'totals':totals,'by_user':by_user,'by_day':by_day,'details':details,'page':page,'pages':pages,'page_size':10,
            'note':'仅记录已登录用户的提问及连接测试。发送与响应Token来自服务商usage；未返回用量的响应标记为未报告，不按零或字符数估算。重试产生的独立响应分别累计。后台共享Agent不计入个人用量。'}


def shadow_usage_statistics(settings,params):
    """Read each persisted attempt once, never recount archived copies."""
    import json
    import sqlite3
    path=settings.runtime/'shadow/experiments.db'
    rows=[]
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
            reports=[json.loads(row[0]) for row in db.execute('SELECT payload FROM experiments')]
        for report in reports:
            for attempt in [report,*report.get('previous_attempts',[])]:
                stamp=attempt.get('started_at') or report['date']
                day=stamp[:10]
                if params.get('from',[''])[0] and day<params['from'][0]:continue
                if params.get('to',[''])[0] and day>params['to'][0]:continue
                usage=attempt.get('usage') or {}
                rows.append({'date':report['date'],'started_at':stamp,'attempt':attempt.get('attempt'),
                             'model':attempt.get('model') or (report.get('model') if attempt is report else '历史记录未保存'),
                             'status':attempt.get('status','unknown'),
                             **{k:_count(usage.get(k)) for k in ('input_tokens','output_tokens','model_calls','unreported_responses')}})
    rows.sort(key=lambda r:r['started_at'],reverse=True)
    totals={key:sum(r[key] or 0 for r in rows) for key in ('input_tokens','output_tokens','model_calls','unreported_responses')}
    totals.update(experiments=len(rows),missing_usage_attempts=sum(r['input_tokens'] is None or r['output_tokens'] is None for r in rows))
    return {'totals':totals,'details':rows,'note':'系统共享任务，独立于个人用量；按执行开始日期筛选，不受租户和用户筛选影响。包含失败及重跑，每次仅统计一次；无用量记录不估算，历史未记录的消耗无法追溯。'}
