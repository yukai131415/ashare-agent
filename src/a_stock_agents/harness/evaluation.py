"""Offline structural acceptance checks. Does not call models or judge investment returns."""
from .persistence import RunStore
from .planning.tool import get_execution_plan


def audit_run(settings,run_id):
    # 这里仅检查“成功任务的阶段是否齐全、依赖是否成功、调用是否待核对”。
    # 使用当前可信计划核对轨迹，不重放历史任务，也不评价选股收益或证据真实性。
    trace=RunStore(settings).trace(run_id);run=trace['run'];plan=get_execution_plan(run['kind'])
    steps={step['step_id']:step for step in trace['steps']};findings=[]
    for specification in plan['steps']:
        result=steps.get(specification['id'])
        if result and result['status']=='succeeded':
            for dependency in specification['depends_on']:
                if steps.get(dependency,{}).get('status')!='succeeded':findings.append('前置阶段未验收：'+specification['id'])
        elif run['status']=='succeeded':findings.append('任务成功但阶段未验收：'+specification['id'])
    uncertain=[call['id'] for call in trace['model_calls'] if call['status'] in ('running','sent','outcome_unknown')]
    if uncertain:findings.append('存在结果或计费待核对的模型调用')
    return {'run_id':run_id,'status':run['status'],'plan_version':run['version'],
            'structural_checks_passed':not findings,'findings':findings,'steps':trace['steps'],
            'model_calls':len(trace['model_calls']),'uncertain_model_calls':len(uncertain),
            'scope':'只验证执行轨迹一致性；不代替行情、财务、证据或收益评估'}
