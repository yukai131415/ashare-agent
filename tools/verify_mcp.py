"""Exercise actual MCP stdio initialization/discovery/tool dispatch (no API keys)."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from a_stock_agents.config import load_settings
from a_stock_agents.contracts import iso_now
from a_stock_agents.storage import atomic_write_json


async def main(live):
    settings=load_settings()
    params=StdioServerParameters(command=sys.executable,args=['-m','a_stock_agents.mcp_server'],
        cwd=str(settings.root),env={**os.environ,'PYTHONPATH':str(settings.root/'src'),'PYTHONIOENCODING':'utf-8'})
    checks=[]
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            listed=await session.list_tools()
            names=[t.name for t in listed.tools]
            assert set(names)=={'get_quotes','get_financials','search_research','sync_research'}
            checks.append({'check':'stdio_initialize_and_discover','ok':True,'tools':names})
            response=await session.call_tool('search_research',{'query':'盈利','code':'000823','as_of':iso_now()})
            assert not response.isError,response
            checks.append({'check':'research_tool_dispatch','ok':True,'note':'Dispatched against the configured real Milvus service'})
            if live:
                for name,args in [('get_quotes',{'codes':['000823']}),('get_financials',{'code':'000823','as_of':iso_now()[:10]})]:
                    response=await session.call_tool(name,args)
                    assert not response.isError,response
                    payload=response.structuredContent
                    if payload is None:
                        payload=json.loads(''.join(item.text for item in response.content if item.type=='text'))
                    if name=='get_quotes':
                        assert payload.get('rows') and payload['rows'][0]['code']=='000823',payload
                        assert payload['rows'][0]['quote_time'],payload
                        summary={'provider':payload['provider'],'fetched_at':payload['fetched_at'],'row':payload['rows'][0]}
                    else:
                        assert payload.get('status')=='ok' and payload['reports'],payload
                        assert payload['reports'][0]['SECURITY_CODE']=='000823',payload
                        summary={'source':payload['source'],'as_of':payload['as_of'],'report':payload['reports'][0]}
                    checks.append({'check':name,'ok':True,'response_summary':summary})
                response=await session.call_tool('sync_research',{'code':'600000','as_of':iso_now(),'limit':1})
                assert not response.isError,response
                body=response.structuredContent or json.loads(''.join(x.text for x in response.content if x.type=='text'))
                assert body.get('documents',0)>0,body
                checks.append({'check':'real_research_sync','ok':True,'sources':body['sources']})
                response=await session.call_tool('search_research',{'query':'盈利与资产质量风险','code':'600000','as_of':iso_now(),'limit':1})
                assert not response.isError,response
                body=response.structuredContent or json.loads(''.join(x.text for x in response.content if x.type=='text'))
                assert body.get('hits') and body['hits'][0]['code']=='600000',body
                checks.append({'check':'real_research_search','ok':True,'source':body['hits'][0]['source'],'title':body['hits'][0]['title']})
    result={'verified_at':iso_now(),'protocol':'MCP stdio','ok':True,'checks':checks}
    atomic_write_json(settings.root/'docs/verification/mcp_smoke.json',result)
    print('MCP checks passed:',len(checks))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true')
    async def bounded():
        async with asyncio.timeout(120):
            await main(parser.parse_args().live)
    asyncio.run(bounded())
