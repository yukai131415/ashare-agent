"""Enforce plan-local tool permissions and archive calls at the execution boundary."""
import json
import uuid
from contextlib import contextmanager
from .runtime import active_harness,active_step
from .persistence import fingerprint
from ..console_log import sanitize
from ..contracts import iso_now


@contextmanager
def tool_boundary(name,arguments):
    runtime=active_harness();step=active_step()
    result={};identifier=uuid.uuid4().hex
    if runtime and step:
        permitted=runtime.steps[step]['allowed_tools']
        if name not in permitted and name!='get_execution_plan':raise PermissionError(f'计划阶段 {step} 不允许工具 {name}')
        cutoff=runtime.inputs.get('as_of') or runtime.inputs.get('cutoff')
        # 此处是日期级防穿越；披露时间、行情时效等更细规则仍由业务工具校验。
        if cutoff and arguments.get('as_of') and str(arguments['as_of'])[:10]>str(cutoff)[:10]:raise PermissionError('工具截止日期晚于任务冻结日期')
    try:
        # 调用方把可归档的结果写入此字典；上下文退出时统一补状态并脱敏保存。
        yield result
    except Exception as exc:
        result.update(status='failed',error_type=type(exc).__name__)
        raise
    else:result['status']='succeeded'
    finally:
        if runtime:
            with runtime.store.connection() as db:
                db.execute('''CREATE TABLE IF NOT EXISTS tool_calls(id TEXT PRIMARY KEY,run_id TEXT,step_id TEXT,name TEXT,status TEXT,argument_hash TEXT,payload TEXT,at TEXT)''')
                db.execute('INSERT INTO tool_calls VALUES(?,?,?,?,?,?,?,?)',(identifier,runtime.run_id,step,name,result.get('status','unknown'),fingerprint(arguments),json.dumps(sanitize({'arguments':arguments,**result}),ensure_ascii=False,default=str),iso_now()))


@contextmanager
def model_boundary(config):
    runtime=active_harness();identifier=uuid.uuid4().hex;details={'status':'prepared'}
    def save():
        if not runtime:return
        with runtime.store.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS model_calls(id TEXT PRIMARY KEY,run_id TEXT,step_id TEXT,model TEXT,status TEXT,payload TEXT,at TEXT)''')
            db.execute('INSERT INTO model_calls VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,payload=excluded.payload,at=excluded.at',
                (identifier,runtime.run_id,active_step(),str(config.get('model','')),details['status'],json.dumps(sanitize({k:v for k,v in details.items() if not k.startswith('_')}),ensure_ascii=False,default=str),iso_now()))
    def mark_sent():
        # 先持久化发送意图，再进行网络 I/O。崩溃窗口可能误判“已发送”，
        # 但宁可暂停核对，也不能因自动重放造成重复计费。
        details['status']='sent';save()
    details['_mark_sent']=mark_sent
    save()
    try:yield details
    except Exception as exc:
        # 未发送和发送后断线必须区分；后者不能据本地异常断言服务端未处理。
        if details['status'] in ('running','sent'):details['status']='outcome_unknown'
        elif details['status']=='prepared':details['status']='not_sent'
        details['error_type']=type(exc).__name__
        raise
    finally:save()
