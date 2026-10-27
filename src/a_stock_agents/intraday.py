"""Next-trading-day observation. Read-only market tools, never sends trade orders."""
import json
import math
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime,timedelta
from typing import Literal

from pydantic import BaseModel,ConfigDict,Field
from .console_log import console_event,log_scope
from .scheduler import SHANGHAI,shanghai_now,process_lock
from .trading_calendar import is_trading_weekday,next_weekday
from .storage import read_json,atomic_write_json
from .llm import llm_config
from .harness.adapters.llm import complete

DISCLAIMER='主力意图仅为情景推测，行情快照无法确认资金身份、真实委托或买卖动机；不是交易指令，不保证收益，也不假定你的持仓或成本。'


class Signal(BaseModel):
    model_config=ConfigDict(extra='forbid')
    observation:str=Field(min_length=1,max_length=200)
    interpretation:str=Field(min_length=1,max_length=250)
    alternative:str=Field(min_length=1,max_length=200)
    evidence_ids:list[str]=Field(min_length=1,max_length=6)


class Analysis(BaseModel):
    model_config=ConfigDict(extra='forbid')
    summary:str=Field(min_length=1,max_length=600)
    signals:list[Signal]=Field(max_length=3)
    risk_level:Literal['low','medium','high','unknown']
    watchpoints:list[str]=Field(max_length=4)
    limitations:list[str]=Field(min_length=1,max_length=4)


def folder(settings):
    path=settings.runtime/'intraday';path.mkdir(parents=True,exist_ok=True);return path


@contextmanager
def connection(settings):
    db=sqlite3.connect(folder(settings)/'observations.db',timeout=10)
    db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS watchlists(day TEXT PRIMARY KEY,payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS observations(day TEXT,slot TEXT,code TEXT,status TEXT,payload TEXT NOT NULL,PRIMARY KEY(day,slot,code))')
    try:
        with db:yield db
    finally:db.close()


def config(settings):
    return {'enabled':bool(read_json(folder(settings)/'config.json',settings.values.get('intraday',{'enabled':True})).get('enabled',True)),
            'interval_minutes':10,'timezone':'Asia/Shanghai','sessions':['09:30–11:30','13:00–15:00'],
            'model':llm_config(settings).get('model'),'source':'前一交易日正式推荐及已完成影子选择，同代码合并'}


def set_enabled(settings,enabled):
    if not isinstance(enabled,bool):raise ValueError('enabled必须为布尔值')
    atomic_write_json(folder(settings)/'config.json',{'enabled':enabled})
    console_event('intraday_config_changed',enabled=enabled)


def session_slot(now):
    now=now.astimezone(SHANGHAI)
    if not is_trading_weekday(now.date()):return None
    minute=now.hour*60+now.minute
    for start,end in ((570,690),(780,900)):
        if start<=minute<=end:
            slot=start+(minute-start)//10*10
            return now.replace(hour=slot//60,minute=slot%60,second=0,microsecond=0)
    return None


def next_slot(now):
    now=now.astimezone(SHANGHAI)
    for days in range(370):
        day=(now+timedelta(days=days)).date()
        if not is_trading_weekday(day):continue
        for minute in [*range(570,691,10),*range(780,901,10)]:
            candidate=datetime(day.year,day.month,day.day,minute//60,minute%60,tzinfo=SHANGHAI)
            if candidate>now:return candidate.isoformat()
    return None


def targets(settings,day):
    from .recommendation_store import recommendation_store
    from .shadow import history
    rows=[]
    for rec in recommendation_store(settings.runtime).history():
        rows.append(('正式推荐',rec,str(rec.get('created_at',''))))
    for report in history(settings):
        if report.get('status')=='completed' and report.get('selection'):
            stock=report['selection']
            rows.append(('影子实验',{'recommendation_date':report['date'],'target_date':report['target_date'],
                          'code':stock['code'],'name':stock.get('name',''),'reference_price':stock.get('price')},report.get('started_at','')))
    result={}
    for source,rec,created in rows:
        try:
            previous=datetime.fromisoformat(rec['recommendation_date']).date()
            stamp=datetime.fromisoformat(created)
            if stamp.tzinfo is None:continue
            made=stamp.astimezone(SHANGHAI).date()
            if rec['target_date']!=day or not is_trading_weekday(previous) or next_weekday(previous).isoformat()!=day or not previous<=made<datetime.fromisoformat(day).date():continue
            code=str(rec['code'])
            if not re.fullmatch(r'(?:60|68|00|30)\d{4}',code):continue
        except (ValueError,KeyError,TypeError):continue
        if code not in result:
            try:reference=float(rec.get('reference_price'));reference=reference if math.isfinite(reference) and reference>0 else None
            except (ValueError,TypeError):reference=None
            result[code]={'code':code,'name':str(rec.get('name','')),'recommendation_date':rec['recommendation_date'],
                          'target_date':day,'reference_price':reference,'sources':[]}
        if source not in result[code]['sources']:result[code]['sources'].append(source)
    return sorted(result.values(),key=lambda item:item['code'])[:2]


def watchlist(settings,day,freeze=False):
    with connection(settings) as db:
        row=db.execute('SELECT payload FROM watchlists WHERE day=?',(day,)).fetchone()
        if row:return json.loads(row['payload'])
    stocks=targets(settings,day)
    if freeze and stocks:
        with connection(settings) as db:
            db.execute('INSERT OR IGNORE INTO watchlists VALUES(?,?)',(day,json.dumps(stocks,ensure_ascii=False)))
            stocks=json.loads(db.execute('SELECT payload FROM watchlists WHERE day=?',(day,)).fetchone()['payload'])
    return stocks


def save(settings,record):
    with connection(settings) as db:
        db.execute('UPDATE observations SET status=?,payload=? WHERE day=? AND slot=? AND code=?',
                   (record['status'],json.dumps(record,ensure_ascii=False,allow_nan=False),record['day'],record['slot'],record['code']))


def records(settings,day):
    with connection(settings) as db:
        return [json.loads(row['payload']) for row in db.execute('SELECT payload FROM observations WHERE day=? ORDER BY slot DESC,code LIMIT 100',(day,))]


def validate_quote(row,code,now):
    if row.get('code')!=code:raise ValueError('行情代码不匹配')
    price=row.get('price')
    if isinstance(price,bool) or not isinstance(price,(float,int)) or not math.isfinite(price) or price<=0:raise ValueError('行情价格无效')
    for key in ('change_pct','amount','turnover_pct','volume_ratio','previous_close','high_price'):
        value=row.get(key)
        if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value)):raise ValueError('行情数值无效：'+key)
    stamp=datetime.strptime(str(row.get('quote_time','')),'%Y%m%d%H%M%S').replace(tzinfo=SHANGHAI)
    age=(now-stamp).total_seconds()
    if stamp.date()!=now.date() or not -5<=age<=120:raise ValueError('行情过期或时间异常，未调用模型')
    return stamp


def fetch_quote(settings,code):
    from .agent_tools import AgentTools
    tools=AgentTools(settings)
    try:
        data=tools.call('get_quotes',{'codes':[code]})
        row=next((r for r in data.get('rows',[]) if r.get('code')==code),None)
        if row is None:raise ValueError('未获取到该股票行情')
        return row
    finally:tools.close()


def analyze(settings,record,previous,transport=complete):
    cfg=llm_config(settings)
    if not cfg.get('enabled') or not cfg.get('api_key'):raise ValueError('系统模型未启用或未配置密钥')
    evidence=[{'id':'quote:current','quote':record['quote']}]
    for index,item in enumerate(previous[:5]):evidence.append({'id':f'quote:previous:{index}','quote':item['quote']})
    record['model']=cfg['model'];record['evidence']=evidence
    save(settings,record)
    usage={}
    def sink(value):
        usage.update(input_tokens=value.get('input_tokens',value.get('prompt_tokens')),
                     output_tokens=value.get('output_tokens',value.get('completion_tokens')))
        record['usage']=dict(usage);save(settings,record)
    tools=[{'type':'function','function':{'name':'submit_review','description':'提交盘中辅助分析，仅研究不交易','parameters':Analysis.model_json_schema()}}]
    messages=[{'role':'system','content':'你是盘中证据分析助手，仅输出submit_review工具调用。依据提供的真实行情和历史快照，区分观察事实、可能解释、替代解释。价格和累计成交额不能证明主力身份、净流入或操纵意图，不编造逐笔/L2/资金数据、新闻或持仓。没有证据时明确无法判断。涨跌、缩放量等比较必须有可比历史快照；首个快照不能假装已有10分钟趋势。提供观察条件和风险，不发布买卖指令、仓位比例或收益承诺。每条signal引用已有evidence_ids。中文简洁，summary少于200字，signals最多3条，watchpoints和limitations每条少于100字。'},
              {'role':'user','content':json.dumps({'target':record['target'],'captured_at':record['captured_at'],
                 'derived':record.get('derived',{}),'evidence':evidence,'limitations':['仅行情快照，无逐笔委托、账户身份和盘口资金净流入','累计成交额不是资金净流入']},ensure_ascii=False)}]
    _,call,value=transport({**cfg,'_usage_sink':sink},messages,tools,True,40)
    if not usage:sink(value or {})
    if call['function']['name']!='submit_review':raise ValueError('模型调用了不允许的工具')
    args=call['function']['arguments'];args=json.loads(args) if isinstance(args,str) else args
    result=Analysis.model_validate(args).model_dump()
    known={item['id'] for item in evidence}
    if any(identifier not in known for signal in result['signals'] for identifier in signal['evidence_ids']):raise ValueError('模型引用了不存在的行情证据')
    return result


def run_once(settings,now=None,quote_fetcher=fetch_quote,transport=complete):
    realtime=now is None
    now=(now or shanghai_now()).astimezone(SHANGHAI)
    if not config(settings)['enabled']:return False
    slot=session_slot(now)
    if slot is None:return False
    with process_lock(folder(settings)/'run.lock') as acquired:
        if not acquired:return False
        day=now.date().isoformat();stocks=watchlist(settings,day,freeze=True)
        for stock in stocks:
            record={'day':day,'slot':slot.isoformat(),'code':stock['code'],'target':stock,'status':'running',
                    'started_at':shanghai_now().isoformat(),'disclaimer':DISCLAIMER}
            with connection(settings) as db:
                # 先以日期/时段/代码唯一键认领，再抓行情和调用模型，避免重复计费。
                # 即使上次进程中断，也不自动重放已认领时段，以免补造旧时点行情。
                inserted=db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)',(day,record['slot'],stock['code'],'running',json.dumps(record,ensure_ascii=False))).rowcount
            if not inserted:continue
            with log_scope(component='intraday',run_id=uuid.uuid4().hex,code=stock['code'],slot=record['slot']):
                console_event('intraday_started',sources=stock['sources'])
                from .harness.control.master import record as control_record
                control_record(settings,record['slot'],'intraday.'+stock['code'],'running')
                from .harness.runtime import WorkflowHarness
                inputs={'day':day,'slot':record['slot'],'code':stock['code'],'target':stock}
                runtime=WorkflowHarness(settings,'intraday:'+record['slot']+':'+stock['code'],'intraday',inputs)
                try:
                    def quote_step():
                        row=quote_fetcher(settings,stock['code'])
                        captured=shanghai_now() if realtime else now
                        # 网络请求跨过十分钟边界后，不能将新时段行情记为旧时段快照。
                        if session_slot(captured)!=slot:raise ValueError('采集已跨时段，不将新行情冒充原时段行情')
                        validate_quote(row,stock['code'],captured)
                        return {'row':row,'captured_at':captured.isoformat()}
                    with runtime.scope():
                        captured_quote=runtime.run_step('quote',inputs,quote_step,verify=lambda value:bool(value.get('row')))
                    row=captured_quote['row'];captured=datetime.fromisoformat(captured_quote['captured_at'])
                    # Public quote fields only; no credentials or private selection features.
                    record['quote']={key:row.get(key) for key in ('code','name','price','change_pct','amount','turnover_pct','volume_ratio','previous_close','high_price','quote_time')}
                    record['captured_at']=captured.isoformat()
                    previous=[r for r in records(settings,day) if r['code']==stock['code'] and r['slot']<record['slot'] and r.get('quote')]
                    record['derived']={}
                    if previous:
                        older=previous[0]['quote'];delta=row.get('amount');base=older.get('amount')
                        record['derived']['price_change_since_previous_pct']=round((row['price']/older['price']-1)*100,4)
                        if isinstance(delta,(int,float)) and isinstance(base,(int,float)) and delta>=base:
                            # 成交额是日内累计值，差值才是区间值；累计值回退时不输出负量。
                            record['derived']['interval_turnover_amount']=delta-base
                        record['derived']['previous_quote_time']=older['quote_time']
                    save(settings,record)
                    console_event('intraday_quote_received',quote=record['quote'],derived=record['derived'])
                    with runtime.scope():
                        output=runtime.run_step('analyze',{'quote':record['quote'],'previous':previous},
                            lambda:{'analysis':analyze(settings,record,previous,transport)},verify=lambda value:bool(value.get('analysis')))
                    record['analysis']=output['analysis']
                    record['status']='completed'
                    def persist_step():
                        save(settings,record);return {'saved':True}
                    with runtime.scope():runtime.run_step('persist',{'day':day,'slot':record['slot'],'code':stock['code']},persist_step,verify=lambda value:value.get('saved') is True)
                    runtime.finish(True)
                except Exception as exc:
                    runtime.finish(False)
                    record.update(status='analysis_failed' if record.get('quote') else 'quote_failed',error_type=type(exc).__name__,message=str(exc)[:500] if isinstance(exc,ValueError) else '数据或模型请求失败，请查看控制台日志')
                    console_event('intraday_failed',error_type=type(exc).__name__,message=record['message'])
                record['finished_at']=shanghai_now().isoformat();save(settings,record)
                console_event('intraday_finished',status=record['status'],usage=record.get('usage'))
                control_record(settings,record['slot'],'intraday.'+stock['code'],
                                'completed' if record['status']=='completed' else 'needs_attention',
                                error_type=record.get('error_type'))
        return bool(stocks)


def view(settings,day=None,admin=False):
    now=shanghai_now();selected=day or now.date().isoformat();datetime.strptime(selected,'%Y-%m-%d')
    rows=records(settings,selected)
    for row in rows:
        if not admin:row.pop('usage',None)
        if row['status']=='running':
            started=datetime.fromisoformat(row['started_at'])
            if (now-started).total_seconds()>180:row.update(status='interrupted',message='上次任务中断或超时；不自动重放该时段以避免重复计费，下一时段继续')
    cfg=config(settings)
    try:upcoming=next_slot(now) if cfg['enabled'] else None
    except ValueError:upcoming=None
    return {'date':selected,'server_now':now.isoformat(),'config':cfg,'watchlist':watchlist(settings,selected),
            'records':rows,'next_slot':upcoming,'disclaimer':DISCLAIMER,
            **({'system_usage':{'input_tokens':sum(r.get('usage',{}).get('input_tokens') or 0 for r in rows),
                                'output_tokens':sum(r.get('usage',{}).get('output_tokens') or 0 for r in rows)}} if admin else {})}


class IntradayScheduler:
    def __init__(self,settings):self.settings=settings;self.stop_event=threading.Event();self.thread=None
    def start(self):
        if self.thread and self.thread.is_alive():return
        self.thread=threading.Thread(target=self.loop,name='intraday-observer',daemon=True);self.thread.start()
        console_event('intraday_scheduler_started',interval_minutes=10,sessions=config(self.settings)['sessions'])
    def stop(self):self.stop_event.set()
    def loop(self):
        while not self.stop_event.is_set():
            try:run_once(self.settings)
            except Exception as exc:console_event('intraday_scheduler_error',error_type=type(exc).__name__)
            self.stop_event.wait(20)
