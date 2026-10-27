"""Read-only research views over archived recommendation inputs."""
from datetime import datetime
from functools import lru_cache
import math
from pathlib import Path

from .agents.market import is_allowed_stock
from .contracts import MarketRow
from .models import get_model, valuation_factor, SCORING_VERSION
from .selection import rank_candidates
from .storage import read_json

LABELS={'momentum_z':'涨跌动量','volume_ratio_z':'量比','liquidity_z':'成交活跃度','turnover_z':'换手率','valuation_z':'估值','amplitude_z':'振幅风险','change_speed_z':'涨速'}
RAW={'momentum_z':'change_pct','volume_ratio_z':'volume_ratio','liquidity_z':'amount','turnover_z':'turnover_pct','valuation_z':'pe_ratio','amplitude_z':'amplitude_pct','change_speed_z':'change_speed_pct'}
WEIGHTS={'balanced':{'momentum_z':.28,'volume_ratio_z':.20,'liquidity_z':.18,'turnover_z':.12,'valuation_z':.05,'amplitude_z':-.08},
         'momentum':{'momentum_z':.44,'volume_ratio_z':.24,'liquidity_z':.12,'change_speed_z':.08},
         'value_reversal':{'momentum_z':-.28,'valuation_z':.28,'liquidity_z':.18,'volume_ratio_z':.10,'amplitude_z':-.08}}


def number(value):
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (ValueError,TypeError):return None


def contributions(features,model,version=SCORING_VERSION):
    if version!=SCORING_VERSION or model not in WEIGHTS:
        return {'status':'unsupported_version','items':[],'message':'该历史评分版本未配置贡献公式，保留原分数。'}
    items=[]
    for key,weight in WEIGHTS[model].items():
        value=valuation_factor(features) if key=='valuation_z' else max(number(features.get(key)) or 0,0) if key=='amplitude_z' else number(features.get(key)) or 0
        items.append({'key':key,'label':LABELS[key],'weight':weight,'factor_value':value,'contribution':round(value*weight,8),
                      'missing_input':RAW[key] in features and number(features.get(RAW[key])) is None})
    return {'status':'ok','items':items,'total':round(sum(row['contribution'] for row in items),6),'version':version,'news_factor_enabled':False}


@lru_cache(maxsize=8)
def _archive(path,mtime):
    return read_json(Path(path),{})


def feature_snapshot(settings,rec,workflow):
    """Match original score/price/factors; do not silently re-score a different snapshot."""
    if not rec:return None,[]
    day=str(rec.get('recommendation_date') or rec.get('created_at',''))[:10]
    folder=settings.runtime/'agents/analysis/data'
    files=sorted(folder.glob('features_'+day.replace('-','')+'_*.json'),reverse=True)
    latest=folder/'latest.json'
    if latest.exists():files.append(latest)
    model=rec.get('model','balanced')
    if rec.get('model_version')!=SCORING_VERSION:return None,[]
    basis=rec.get('basis') or {}
    for path in files:
        payload=_archive(str(path),path.stat().st_mtime_ns)
        if str(payload.get('generated_at',''))[:10]!=day:continue
        rows=payload.get('features',[])
        found=next((row for row in rows if row.get('code')==rec.get('code')),None)
        if not found:continue
        if abs((number(found.get('price')) or 0)-(number(rec.get('reference_price')) or 0))>.00001:continue
        if abs(get_model(model).score(found)-(number(rec.get('score')) or 0))>.000002:continue
        if any(key in basis and number(found.get(key))!=number(basis[key]) for key in LABELS):continue
        return {'path':str(path.relative_to(settings.root)),'generated_at':payload.get('generated_at'),'count':len(rows)},rows
    return None,[]


def market_mood(rows,source):
    valid=[row for row in rows if number(row.get('change_pct')) is not None and (number(row.get('price')) or 0)>0]
    if not valid:return {'status':'unavailable','message':'没有与所选推荐匹配的行情快照。'}
    changes=[float(row['change_pct']) for row in valid]
    up=sum(v>0 for v in changes);down=sum(v<0 for v in changes)
    bins=[('低于-5%',lambda v:v < -5),('-5%至-2%',lambda v:-5<=v < -2),('-2%至0%',lambda v:-2<=v < 0),('持平',lambda v:v==0),('0%至2%',lambda v:0<v<=2),('2%至5%',lambda v:2<v<=5),('高于5%',lambda v:v>5)]
    amount=sum(number(row.get('amount')) or 0 for row in valid)
    amount_known=sum(number(row.get('amount')) is not None for row in valid)
    ratio_known=sum(number(row.get('volume_ratio')) is not None for row in valid)
    return {'status':'ok','scope':'已采集股票池，不代表全市场；不是实时推送','source':source,'total':len(valid),'up':up,'down':down,'flat':len(valid)-up-down,
            'advance_ratio':round(up/len(valid)*100,2),'average_change_pct':round(sum(changes)/len(changes),3),'amount_yi':round(amount/1e8,2) if amount_known else None,
            'amount_known_count':amount_known,'volume_ratio_known_count':ratio_known,
            'active_volume_ratio_count':sum((number(row.get('volume_ratio')) or 0)>1 for row in valid) if ratio_known else None,
            'distribution':[{'label':label,'count':sum(test(v) for v in changes)} for label,test in bins]}


def research_view(settings,data):
    rec=data.get('recommendation');workflow=data.get('workflow') or {}
    source,rows=feature_snapshot(settings,rec,workflow)
    ranked=[]
    if rows:
        allowed=[row for row in rows if is_allowed_stock(MarketRow(code=str(row['code']),name=str(row['name']),price=1,change_pct=0),settings.section('market'))]
        ranked=rank_candidates(allowed,settings.section('recommendation'),rec['model'])
    candidates=[]
    for rank,(score,row) in enumerate(ranked,1):
        candidates.append({**{key:row.get(key) for key in ('code','name','price','change_pct','amount','turnover_pct','volume_ratio','amplitude_pct','pe_ratio')},
                           'rank':rank,'score':round(score,6),'contributions':contributions(row,rec['model'],rec.get('model_version'))})
    if not candidates:
        candidates=[{**row,'rank':i+1,'contributions':{'status':'unavailable','items':[]}} for i,row in enumerate(workflow.get('roles',{}).get('technical',{}).get('top5',[]))]
    selected=next((row for row in candidates if rec and row['code']==rec['code']),None)
    contrib=selected.get('contributions') if selected else contributions(rec.get('basis',{}),rec.get('model',''),rec.get('model_version')) if rec else {'status':'unavailable','items':[]}
    if contrib and contrib.get('status')=='ok' and abs(contrib['total']-(number(rec.get('score')) or 0))>.000002:
        contrib={'status':'mismatch','items':[],'message':'因子与历史总分不一致，暂不展示推算贡献。'}
    lead=candidates[:2]
    comparison=None
    if len(lead)==2 and all(row['contributions'].get('status')=='ok' for row in lead):
        comparison={'first':lead[0]['name'],'second':lead[1]['name'],'score_gap':round(lead[0]['score']-lead[1]['score'],6),
                    'items':[{'label':a['label'],'difference':round(a['contribution']-b['contribution'],8)} for a,b in zip(lead[0]['contributions']['items'],lead[1]['contributions']['items'])]}
    roles=workflow.get('roles',{})
    facts=[];risks=[];financials=[];citations=[]
    if rec:
        basis=rec.get('basis',{})
        facts=[f"{rec['name']}（{rec['code']}），参考价 {rec.get('reference_price')} 元，模型评分 {rec.get('score')}。",
               f"策略为 {rec.get('model_label',rec.get('model'))}，目标日 {rec.get('target_date')}；新闻因子未计分。"]
        if number(basis.get('amount')) is not None:facts.append(f"快照成交额 {float(basis['amount'])/1e8:.2f} 亿元，换手率 {basis.get('turnover_pct','未记录')}%。")
        risks=list(roles.get('risk',{}).get('warnings',[]))
        if (number(basis.get('turnover_pct')) or 0)>30 and not any('换手' in str(risk) for risk in risks):risks.append('换手较高，短线波动和交易拥挤风险需关注。')
        if (number(basis.get('pe_ratio')) or 0)>100 and not any('估值' in str(risk) or '市盈率' in str(risk) for risk in risks):risks.append('市盈率较高，当前估值对应较强的盈利预期。')
        for evidence in roles.get('fundamental',{}).get('evidence',[]):
            if evidence.get('code')==rec['code']:
                fin=evidence.get('financials',{})
                for report in [r for r in fin.get('reports',[]) if str(r.get('NOTICE_DATE') or '')[:10] and str(r['NOTICE_DATE'])[:10]<=str(data.get('selected_date',''))][:2]:
                    financials.append({**{key:report.get(key) for key in ('REPORTDATE','NOTICE_DATE','BASIC_EPS','WEIGHTAVG_ROE','TOTAL_OPERATE_INCOME','PARENT_NETPROFIT','SJLTZ','YSTZ')},'source':fin.get('source'),'fetched_at':fin.get('fetched_at')})
        citations=[{key:hit.get(key) for key in ('title','source','text','published_at')} for hit in roles.get('decision',{}).get('research',{}).get('hits',[])[:3]]
    return {'selected_date':data.get('selected_date'),'mood':market_mood(rows,source),'candidates':candidates,
            'ranking':{'total':len(candidates),'source':source,'full_snapshot':bool(rows),'message':'沿用原模型，仅展示归档快照；排名不改写当天推荐。'},
            'contributions':contrib,'comparison':comparison,
            'brief':{'date':data.get('selected_date'),'code':rec.get('code') if rec else None,'name':rec.get('name') if rec else None,
                     'generated_at':workflow.get('finished_at') or (rec or {}).get('created_at'),'facts':facts,'risks':risks,'financials':financials,'citations':citations,
                     'missing':[name for name,value in [('财务披露',financials),('研报引用',citations)] if not value],
                     'source':'已保存的推荐、财务和研报证据归纳，非新一轮选股'}}
