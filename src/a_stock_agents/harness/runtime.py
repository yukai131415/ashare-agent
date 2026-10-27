"""Run approved steps, verify outcomes and reuse only matching successful checkpoints."""
import json
from contextvars import ContextVar
from contextlib import contextmanager
from .planning.tool import get_execution_plan
from .persistence import RunStore,fingerprint
from ..console_log import console_event,log_scope

# 上下文变量把当前任务/阶段传给深层工具和模型网关，避免显式逐层传参。
# 它们不是跨线程全局变量；提交线程池任务时，调用方须使用 copy_context 传播。
_active=ContextVar('harness_active',default=None)
_step=ContextVar('harness_step',default=None)


class ReviewRequired(RuntimeError):pass


class WorkflowHarness:
    def __init__(self,settings,run_id,kind,inputs):
        self.settings,self.run_id=settings,run_id
        self.plan=get_execution_plan(kind);self.store=RunStore(settings)
        # 不只绑定版本名：同名版本的计划内容被修改，也不能复用旧检查点。
        self.inputs={**inputs,'_plan_hash':fingerprint(self.plan)}
        self.store.begin(run_id,self.plan,self.inputs)
        self.steps={s['id']:s for s in self.plan['steps']}

    @contextmanager
    def scope(self):
        token=_active.set(self)
        try:
            with log_scope(run_id=self.run_id,harness_version=self.plan['version']):yield self
        # 恢复进入前的上下文，防止嵌套调用或下一项任务串用本次运行编号。
        finally:_active.reset(token)

    def run_step(self,identifier,state,execute,verify=None):
        from .locking import process_lock
        step=self.steps[identifier]
        # 阻断后仍交给原角色生成 skipped 状态；并非允许后续阶段继续业务执行。
        if state.get('blocked'):return execute()
        for dependency in step['depends_on']:
            checkpoint=self.store.checkpoint(self.run_id,dependency)
            if not checkpoint or checkpoint['status']!='succeeded':raise ReviewRequired('前置阶段尚未通过验收：'+dependency)
        digest=fingerprint(state)
        lock=self.settings.runtime/'harness/locks'/f'{fingerprint([self.run_id,identifier])}.lock'
        # 读取检查点与执行都放在同一把锁内，避免两个进程同时判定“尚未执行”。
        with process_lock(lock) as acquired:
            if not acquired:raise ReviewRequired('阶段正在其他执行器运行，拒绝重复执行')
            old=self.store.checkpoint(self.run_id,identifier)
            # 模型可能已执行并计费，只是回执丢失；这种情况优先于成功检查点判断。
            if old and self.store.uncertain_model_calls(self.run_id,identifier):
                self.store.transition(self.run_id,identifier,'needs_review',digest)
                raise ReviewRequired('模型调用结果或计费待确认，禁止自动重放')
            if old and old['status']=='succeeded':
                if old['input_hash']!=digest:raise ReviewRequired('检查点输入已变化，不能复用旧输出')
                console_event('harness_checkpoint_reused',step=identifier)
                return json.loads(old['output_json'])
            # 发布有外部副作用，失败记录不等于发布未发生，不能按普通读操作重试。
            if old and step['effect']=='publish':
                self.store.transition(self.run_id,identifier,'needs_review',digest)
                self.store.finish(self.run_id,'needs_review')
                raise ReviewRequired('发布阶段执行结果待核对，禁止自动重放')
            self.store.transition(self.run_id,identifier,'running',digest)
            console_event('harness_step_started',step=identifier,acceptance=step['acceptance'])
            try:
                token=_step.set(identifier)
                try:result=execute()
                finally:_step.reset(token)
                # “函数返回”不是成功：正式角色检查状态，影子/盘中提供专属验收器。
                ok=verify(result) if verify else not result.get('blocked') and result.get('roles',{}).get(identifier,{}).get('status')=='success'
                self.store.transition(self.run_id,identifier,'succeeded' if ok else 'failed',digest,result)
                console_event('harness_step_verified',step=identifier,ok=ok)
                if verify and not ok:raise ReviewRequired('阶段结果未通过验收：'+identifier)
                return result
            except Exception:
                self.store.transition(self.run_id,identifier,'needs_review' if step['effect']=='publish' else 'failed',digest)
                raise

    def finish(self,ok):
        if ok and any((self.store.checkpoint(self.run_id,s) or {}).get('status')!='succeeded' for s in self.steps):raise ReviewRequired('任务仍有未验收阶段，不能标记成功')
        status='succeeded' if ok else 'failed'
        if not ok and any((self.store.checkpoint(self.run_id,s) or {}).get('status')=='needs_review' for s in self.steps):status='needs_review'
        self.store.finish(self.run_id,status)


def active_harness():return _active.get()


def active_step():return _step.get()
