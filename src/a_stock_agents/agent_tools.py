"""Typed Function Calling tools shared by LangGraph roles and the MCP server."""
from __future__ import annotations

import hashlib
import re
import uuid
import time
from dataclasses import asdict
from datetime import date
from datetime import datetime, timezone
from typing import Any, Callable
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field
from .agents.market import parse_tencent_quotes
from .contracts import iso_now
from .http import HttpClient
from .research import ResearchIndex
from .storage import atomic_write_json, read_csv, read_json


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')


class FinancialArgs(EmptyArgs):
    code: str = Field(pattern=r'^\d{6}$')
    as_of: date


class QuoteArgs(EmptyArgs):
    codes: list[str] = Field(min_length=1,max_length=50)


class ResearchArgs(EmptyArgs):
    query: str = Field(min_length=1,max_length=2000)
    code: str = Field(pattern=r'^\d{6}$')
    as_of: str
    limit: int = Field(default=3,ge=1,le=10)


class ResearchSyncArgs(EmptyArgs):
    code: str = Field(pattern=r'^\d{6}$')
    as_of: str
    limit: int = Field(default=3,ge=1,le=5)


class AgentTools:
    def __init__(self, settings, runner: Callable | None = None, event: Callable | None = None):
        self.settings, self.runner, self.event = settings, runner, event or (lambda _:None)
        self.calls = []
        self.research = ResearchIndex(settings)
        self.tools = {}
        definitions = [
            ('get_market_snapshot', self.get_market_snapshot, EmptyArgs, '读取本轮采集保存的完整行情快照，不重新抓取。'),
            ('get_quotes', self.get_quotes, QuoteArgs, '查询腾讯实时个股报价，返回行情时间。'),
            ('get_financials', self.get_financials, FinancialArgs, '查询指定日期以前已披露的东方财富财务指标，保留公告日期与来源。'),
            ('search_research', self.search_research, ResearchArgs, '从 Milvus 检索指定股票和时间以前的研报片段及来源。'),
            ('sync_research', self.sync_research, ResearchSyncArgs, '抓取东方财富真实个股研报正文、归档并写入Milvus；不影响选股评分。'),
        ]
        if runner:
            definitions += [
                ('collect_market', self.collect_market, EmptyArgs, '运行已有行情采集 Agent 并执行原有覆盖率校验。'),
                ('collect_news', self.collect_news, EmptyArgs, '运行已有新闻归档 Agent，新闻不参与评分。'),
            ]
        for name, fn, schema, description in definitions:
            self.tools[name] = StructuredTool.from_function(fn, name=name, description=description, args_schema=schema)
        from .harness.planning.tool import execution_plan_tool
        self.tools['get_execution_plan']=execution_plan_tool

    def call(self, name, arguments=None):
        from .console_log import console_event
        started=time.monotonic()
        identifier = uuid.uuid4().hex
        call = {'id':identifier,'name':name,'arguments':arguments or {},'started_at':iso_now()}
        self.event({'status':'tool_started','tool':name,'call_id':identifier})
        console_event('tool_started',tool=name,call_id=identifier,arguments=arguments or {})
        try:
            if name not in self.tools:
                raise ValueError('Unknown tool: '+name)
            from .harness.gateway import tool_boundary
            with tool_boundary(name,arguments or {}) as artifact:
                result = self.tools[name].invoke(arguments or {})
                artifact['result']=result
            call['ok'] = True
            console_event('tool_result',tool=name,call_id=identifier,status=result.get('status') if isinstance(result,dict) else 'returned',
                          result=result,
                          counts={k:len(v) for k,v in result.items() if isinstance(v,list)} if isinstance(result,dict) else {})
            return result
        except Exception as exc:
            call['ok'] = False
            call['error_type'] = type(exc).__name__
            import traceback
            console_event('tool_failed',tool=name,call_id=identifier,error_type=type(exc).__name__,message=str(exc),traceback=traceback.format_exc())
            raise
        finally:
            call['finished_at'] = iso_now()
            self.calls.append(call)
            console_event('tool_finished',tool=name,call_id=identifier,ok=call.get('ok',False),duration_ms=round((time.monotonic()-started)*1000))
            self.event({'status':'tool_finished','tool':name,'call_id':identifier,'ok':call.get('ok',False)})

    def schemas(self):
        """Ready for a compatible chat model's tools parameter / bind_tools."""
        return [convert_to_openai_tool(tool) for tool in self.tools.values()]

    def dispatch_function_call(self, call):
        """Dispatch the name/JSON arguments emitted by a model; never eval model text."""
        import json
        function=call.get('function',call)
        arguments=function.get('arguments',function.get('args',{}))
        if isinstance(arguments,str):
            arguments=json.loads(arguments)
        if not isinstance(arguments,dict):
            raise ValueError('Tool arguments must be a JSON object')
        return self.call(function['name'],arguments)

    def collect_market(self):
        return self.runner('market').to_dict()

    def collect_news(self):
        return self.runner('news').to_dict()

    def get_market_snapshot(self):
        path = self.settings.runtime/'agents/market/data/latest.csv'
        return {'source':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'rows':read_csv(path)}

    def http(self):
        config=self.settings.section('http')
        return HttpClient(timeout=float(self.settings.values.get('workflow',{}).get('tool_timeout_seconds',8)),user_agent=config['user_agent'],interval=0)

    def get_quotes(self, codes):
        if any(not re.fullmatch(r'(?:60\d{4}|68\d{4}|00\d{4}|30\d{4})',c) for c in codes):
            raise ValueError('Unsupported stock code')
        symbols = [('sh' if c.startswith(('6','9')) else 'sz')+c for c in codes]
        url=self.settings.section('market').get('quote_base_url','https://qt.gtimg.cn/')
        text=self.http().get_text(url,{'q':','.join(symbols)})
        return {'provider':'tencent','fetched_at':iso_now(),'rows':[asdict(r) for r in parse_tencent_quotes(text)]}

    def get_financials(self, code, as_of):
        if not re.fullmatch(r'\d{6}',str(code)):raise ValueError('无效股票代码')
        day = as_of.isoformat() if isinstance(as_of,date) else date.fromisoformat(as_of).isoformat()
        cache=self.settings.runtime/'agents/fundamental/cache'/f'{code}_{day}.json'
        saved=read_json(cache,{})
        try:
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(saved['fetched_at'])).total_seconds()
            if 0<=age<1800 and saved.get('code')==code and saved.get('as_of')==day and saved.get('status')=='ok':
                reports=saved.get('reports') or []
                if reports and all(str(r.get('SECURITY_CODE'))==code and str(r.get('NOTICE_DATE',''))[:10] and str(r['NOTICE_DATE'])[:10]<=day for r in reports):
                    return {**saved,'cached':True}
        except (KeyError,ValueError,TypeError):pass
        url='https://datacenter-web.eastmoney.com/api/data/v1/get'
        params={'reportName':'RPT_LICO_FN_CPD','columns':'ALL','pageSize':10,'pageNumber':1,
                'sortColumns':'REPORTDATE','sortTypes':'-1',
                'filter':f'(SECURITY_CODE="{code}")(NOTICE_DATE<=\'{day} 23:59:59\')'}
        from .financial_transport import fetch_financial_json,FinancialUnavailable
        try:payload,attempts=fetch_financial_json(lambda:self.http().get_json(url,params),url,params)
        except FinancialUnavailable as exc:
            atomic_write_json(self.settings.runtime/'agents/fundamental/errors'/f'{code}_{day}_{uuid.uuid4().hex[:8]}.json',
                              {'code':code,'as_of':day,'source':url,'at':iso_now(),'category':exc.category,'attempts':exc.attempts})
            raise
        stamp=iso_now()
        path=self.settings.runtime/'agents/fundamental/raw'/f'{code}_{day}_{uuid.uuid4().hex[:8]}.json'
        atomic_write_json(path,{'source':url,'params':params,'fetched_at':stamp,'payload':payload,'attempts':attempts})
        if not payload.get('success'):
            raise ValueError('财务接口未返回成功结果')
        rows=(payload.get('result') or {}).get('data') or []
        # Independently enforce time/code bounds, even if the provider ignores filters.
        rows=[r for r in rows if str(r.get('SECURITY_CODE'))==code and str(r.get('NOTICE_DATE',''))[:10] and str(r['NOTICE_DATE'])[:10]<=day]
        result={'status':'ok' if rows else 'no_disclosed_data','code':code,'as_of':day,'source':url,'fetched_at':stamp,'raw_path':str(path),'cached':False,'attempts':attempts,
                'reports':[{k:r.get(k) for k in ('SECURITY_CODE','SECURITY_NAME_ABBR','REPORTDATE','NOTICE_DATE','EITIME','BASIC_EPS','WEIGHTAVG_ROE','TOTAL_OPERATE_INCOME','PARENT_NETPROFIT','SJLTZ','YSTZ')} for r in rows]}
        if rows:atomic_write_json(cache,result)
        return result

    def search_research(self, query, code, as_of, limit=3):
        return self.research.search(query,code,as_of,limit)

    def sync_research(self,code,as_of,limit=3):
        if not self.research.enabled:return {'status':'disabled','documents':0}
        from .research_collector import ResearchCollector
        return ResearchCollector(self.settings,self.research).sync(code,as_of,limit)

    def close(self):
        self.research.close()
