"""Manual live MCP verification; no recommendation workflow or LLM calls."""
import asyncio
import json
import os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from a_stock_agents.config import load_settings
from a_stock_agents.external_stock_mcp import stock_observation
from a_stock_agents.contracts import iso_now
from a_stock_agents.storage import atomic_write_json


async def discover(settings):
    command=settings.runtime/'stock-mcp-venv/Scripts/stock-data-mcp.exe'
    cwd=settings.runtime/'external_stock_mcp';cwd.mkdir(parents=True,exist_ok=True)
    names=('PATH','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','APPDATA','LOCALAPPDATA')
    env={k:os.environ[k] for k in names if k in os.environ}
    env.update(PYTHONIOENCODING='utf-8',PYTHONUTF8='1',TRANSPORT='stdio')
    async with asyncio.timeout(55):
        with (cwd/'verification.stderr.log').open('a',encoding='utf-8') as errors:
            async with stdio_client(StdioServerParameters(command=str(command),args=[],cwd=str(cwd),env=env),errlog=errors) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    tools=await session.list_tools()
                    expected={'stock_fund_flow','stock_zt_pool','stock_lhb_ggtj_sina','stock_sector_fund_flow_rank'}
                    found={tool.name for tool in tools.tools}
                    return {'ok':expected<=found,'tools':sorted(expected&found),'total_tools':len(found)}


if __name__=='__main__':
    settings=load_settings()
    report={'verified_at':iso_now(),'protocol':'MCP stdio','source_commit':'58f488772eefc6ac2192182fa67517d90922469e'}
    try:report['discovery']=asyncio.run(discover(settings))
    except Exception as exc:report['discovery']={'ok':False,'error_type':type(exc).__name__}
    print(json.dumps(report,ensure_ascii=False),flush=True)
    report['queries']=[]
    if report['discovery']['ok']:
        for kind,params in [('funds',{'code':'000530'}),('limitpool',{}),('billboard',{}),('sectorfunds',{})]:
            result=stock_observation(settings,kind,params)
            summary={k:result.get(k) for k in ('status','tool','message','returned_rows','data_dates','fetched_at','metadata')}
            summary['kind']=kind;report['queries'].append(summary)
            print(json.dumps(summary,ensure_ascii=False),flush=True)
    atomic_write_json(settings.root/'docs/verification/external_stock_mcp.json',report)
