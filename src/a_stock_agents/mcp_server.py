"""Expose the same typed, bounded data tools over MCP stdio."""
import asyncio
import logging
import pymilvus  # Initialize native dependencies on the main thread before the asyncio server.
from mcp.server.fastmcp import FastMCP
from .agent_tools import AgentTools
from .config import load_settings


def create_server(settings=None):
    tools=AgentTools(settings or load_settings())
    server=FastMCP('a-stock-research')

    @server.tool()
    async def get_execution_plan(task_type:str='formal')->dict:
        """只读查看 formal/shadow/intraday 计划的执行步骤、依赖和验收条件；不启动任务。"""
        return await asyncio.to_thread(tools.call,'get_execution_plan',{'task_type':task_type})

    @server.tool()
    async def get_quotes(codes:list[str])->dict:
        """查询腾讯实时行情（最多50只）。"""
        return await asyncio.to_thread(tools.call,'get_quotes',{'codes':codes})

    @server.tool()
    async def get_financials(code:str,as_of:str)->dict:
        """查询 as_of 当天以前已公告的财务指标，日期格式YYYY-MM-DD。"""
        return await asyncio.to_thread(tools.call,'get_financials',{'code':code,'as_of':as_of})

    @server.tool()
    async def search_research(query:str,code:str,as_of:str,limit:int=3)->dict:
        """检索Milvus研报；as_of必须包含时区，返回原文与来源。"""
        logging.getLogger(__name__).info('MCP research tool started for %s',code)
        result=await asyncio.to_thread(tools.call,'search_research',{'query':query,'code':code,'as_of':as_of,'limit':limit})
        logging.getLogger(__name__).info('MCP research tool finished for %s',code)
        return result

    @server.tool()
    async def sync_research(code:str,as_of:str,limit:int=3)->dict:
        """抓取真实个股研报正文并归档到本机Milvus；不更改股票推荐。"""
        return await asyncio.to_thread(tools.call,'sync_research',{'code':code,'as_of':as_of,'limit':limit})

    return server


def main():
    import sys
    from .console_log import console_output
    with console_output(sys.stderr):create_server().run(transport='stdio')


if __name__=='__main__':
    main()
