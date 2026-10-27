"""Bounded question evidence, excluding duplicated full-pipeline diagnostics."""
import json


def bounded(value,depth=0):
    if depth>5:return '层级过深，未附完整内容'
    if isinstance(value,dict):return {str(k):bounded(v,depth+1) for k,v in list(value.items())[:20]}
    if isinstance(value,list):return [bounded(v,depth+1) for v in value[:3]]
    if isinstance(value,str):return value[:1000]+('…（已节选）' if len(value)>1000 else '')
    return value


def question_evidence(data,news_lines):
    rec=data.get('recommendation') or {};workflow=data.get('workflow') or {}
    roles={}
    keep=('title','status','market_rows','model','model_version','candidate_count','top5','scope','affects_ranking','passed','reviewed_code','checks','warnings','ranking_policy','daily_lock_preserved','explanation')
    for name,role in (workflow.get('roles') or {}).items():
        if not isinstance(role,dict):continue
        compact={key:bounded(role[key]) for key in keep if key in role}
        if name=='fundamental':
            evidence=role.get('evidence') or []
            compact['evidence']=bounded([item for item in evidence if isinstance(item,dict) and item.get('code')==rec.get('code')][:1])
        if name=='decision':compact['research']=bounded(role.get('research'))
        roles[name]=compact
    bt=data.get('backtest') or {}
    context={'date':data.get('selected_date'),'recommendation':bounded(rec),'strategy':bounded(data.get('strategy')),
             'agent_research':{'engine':workflow.get('engine'),'ok':workflow.get('ok'),'blocked':bounded(workflow.get('blocked')),'roles':roles},
             'backtest':{key:bt.get(key) for key in ('wins','losses','total','win_rate')},
             'selected_backtest':bounded(rec.get('backtest')),'news':bounded(news_lines),
             'context_scope':'仅包含所选推荐与角色摘要；全量行情、其他股票财务、诊断日志及全部历史回测未发送。不得据此重新选股或虚构缺失项。',
             'news_scoring_note':'所选推荐使用历史版本，新闻情绪参与当时评分；当前新推荐已禁用新闻因子。' if (rec.get('basis') or {}).get('news_factor_enabled',True) else '所选推荐已禁用新闻因子；新闻仅供阅读，不是本次选股加分或扣分的依据。'}
    encoded=json.dumps(context,ensure_ascii=False,separators=(',',':'))
    if len(encoded)>18000:
        context['agent_research']['roles']={name:{k:v for k,v in role.items() if k not in ('evidence','research','explanation')} for name,role in roles.items()}
        context['context_scope']+=' 因篇幅限制，未附研报及解释全文。'
        encoded=json.dumps(context,ensure_ascii=False,separators=(',',':'))
    return encoded
