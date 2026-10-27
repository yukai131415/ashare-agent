"""Read-only, allowlisted Stock Data MCP client. Never participates in scoring."""
import asyncio
import csv
import io
import json
import os
import re
import threading
import time
from datetime import date, timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .contracts import iso_now
from .scheduler import shanghai_now, is_trading_weekday
from .console_log import console_event

KINDS = {'funds', 'limitpool', 'billboard', 'sectorfunds'}
_lock = threading.Lock()
_cache = {}


def request_spec(kind, params):
    if kind not in KINDS:
        raise ValueError('不支持的观察类型')
    def value(key, default):
        v = params.get(key, [default])
        return str(v[0] if isinstance(v, list) else v)
    if kind == 'funds':
        code = value('code', '000530')
        if not re.fullmatch(r'(?:60|68|00|30)\d{4}', code):
            raise ValueError('请输入有效的6位A股代码')
        return 'stock_fund_flow', {'symbol': code}, '个股近期资金流，日期以返回记录为准；与顶部推荐日期独立。'
    if kind == 'limitpool':
        day = value('date', '')
        if not day:
            target = shanghai_now().date()
            while not is_trading_weekday(target):
                target -= timedelta(days=1)
            day = target.strftime('%Y%m%d')
        try:
            target = date.fromisoformat(day) if '-' in day else date.fromisoformat(f'{day[:4]}-{day[4:6]}-{day[6:]}')
        except ValueError:
            raise ValueError('日期格式应为YYYY-MM-DD') from None
        if target > shanghai_now().date() or not is_trading_weekday(target):
            raise ValueError('请选择已到达的交易日')
        pool = value('pool', '涨停')
        if pool not in ('涨停', '强势'):
            raise ValueError('仅支持涨停或强势股池')
        return 'stock_zt_pool', {'pool_type': pool, 'date': target.strftime('%Y%m%d'), 'limit': 100}, '指定日期股池，最多返回100条；非全部市场，不构成推荐。'
    if kind == 'billboard':
        days = value('days', '5')
        if days not in ('5', '10', '30', '60'):
            raise ValueError('龙虎榜仅支持5/10/30/60日统计')
        return 'stock_lhb_ggtj_sina', {'days': days, 'limit': 100}, f'上游最近{days}日上榜统计，最多100条；不是指定历史日期的单日龙虎榜。'
    days, category = value('days', '5日'), value('category', '行业资金流')
    if days not in ('今日', '5日', '10日') or category not in ('行业资金流', '概念资金流', '地域资金流'):
        raise ValueError('板块统计参数无效')
    if days == '今日' and not is_trading_weekday(shanghai_now().date()):
        raise ValueError('非交易日请选择5日或10日口径，不将旧数据标为今日')
    return 'stock_sector_fund_flow_rank', {'days': days, 'cate': category}, '上游板块资金流选样（可能仅返回头尾各20条），不代表完整板块排名；保留原始单位。'


def parse_result(result):
    if result.isError:
        raise ValueError('上游MCP工具返回执行错误')
    parts = [item.text for item in result.content if getattr(item, 'type', '') == 'text']
    text = '\n'.join(parts).strip()
    if len(text) > 2_000_000:
        raise ValueError('上游结果过大，已拒绝展示')
    # FastMCP may encode a string as a JSON string content block.
    try:
        decoded = json.loads(text)
        if isinstance(decoded, str): text = decoded
    except (ValueError, TypeError): pass
    lines = text.splitlines()
    metadata = [line.lstrip('# ').strip() for line in lines if line.startswith('#')]
    body = '\n'.join(line for line in lines if not line.startswith('#')).strip()
    if not body or body.startswith(('获取', 'Not Found', 'Error', '错误')):
        raise ValueError('上游暂无可用数据或数据源请求失败，请稍后重试')
    reader = csv.reader(io.StringIO(body))
    values = list(reader)
    if not values or len(values[0]) < 2:
        raise ValueError('上游返回格式与CSV表格不符，未生成模拟数据')
    columns = values[0]
    rows = [dict(zip(columns, row)) for row in values[1:501] if len(row) == len(columns)]
    if not rows:
        raise ValueError('上游未返回有效记录')
    dates = sorted({str(v) for row in rows for k, v in row.items() if '日期' in k and v})
    return {'columns': columns, 'rows': rows, 'metadata': metadata,
            'data_dates': dates[:20], 'returned_rows': len(rows)}


async def _call(settings, tool, arguments):
    executable = settings.runtime / 'stock-mcp-venv' / ('Scripts/stock-data-mcp.exe' if os.name == 'nt' else 'bin/stock-data-mcp')
    if not executable.is_file():
        raise ValueError('Stock Data MCP未安装，请按项目接入文档安装独立环境')
    # Do not forward model keys, tenant configuration, strategies or user data.
    names = ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA',
             'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'TUSHARE_TOKEN')
    env = {k: os.environ[k] for k in names if k in os.environ}
    env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', TRANSPORT='stdio')
    cwd = settings.runtime / 'external_stock_mcp'
    cwd.mkdir(parents=True, exist_ok=True)
    params = StdioServerParameters(command=str(executable), args=[], env=env, cwd=str(cwd))
    async with asyncio.timeout(55):
        with (cwd / 'server.stderr.log').open('a', encoding='utf-8') as error_log:
            async with stdio_client(params, errlog=error_log) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    if tool not in {item.name for item in listed.tools}:
                        raise ValueError('已安装的MCP版本不支持所需工具，请检查固定仓库版本')
                    result = await session.call_tool(tool, arguments)
                    parsed = parse_result(result)
                    if tool == 'stock_sector_fund_flow_rank' and not any(re.search('资金|净流|净额', column) for column in parsed['columns']):
                        raise ValueError('上游只返回板块行情，缺少资金流字段，未将涨跌幅冒充资金流')
                    return parsed


def stock_observation(settings, kind, params):
    tool, arguments, scope = request_spec(kind, params)
    key = (str(settings.root), tool, json.dumps(arguments, sort_keys=True))
    def cached():
        item = _cache.get(key)
        return {**item[1], 'cached': True} if item and time.monotonic() - item[0] < 180 else None
    hit = cached()
    if hit: return hit
    if not _lock.acquire(timeout=.1):
        return {'status': 'busy', 'message': '外部数据正在查询，请稍后重试', 'rows': []}
    started = time.monotonic()
    try:
        hit = cached()
        if hit: return hit
        console_event('external_mcp_request',tool=tool,arguments=arguments,timeout_seconds=55)
        result = asyncio.run(_call(settings, tool, arguments))
        payload = {'status': 'ok', 'provider': 'Stock Data MCP', 'tool': tool, 'arguments': arguments,
                   'fetched_at': iso_now(), 'scope': scope, 'cached': False,
                   'freshness_note': '查询时间不等于行情时间；未返回数据日期时不推定为今日或实时。', **result}
        if len(_cache) >= 128: _cache.pop(next(iter(_cache)))
        _cache[key] = (time.monotonic(), payload)
        console_event('external_mcp_finished', tool=tool, ok=True, rows=len(result['rows']),response=result, duration_ms=round((time.monotonic()-started)*1000))
        return payload
    except Exception as exc:
        import traceback
        console_event('external_mcp_finished', tool=tool, ok=False, error_type=type(exc).__name__,message=str(exc),traceback=traceback.format_exc(),duration_ms=round((time.monotonic()-started)*1000))
        return {'status': 'unavailable', 'provider': 'Stock Data MCP', 'tool': tool, 'rows': [],
                'message': str(exc) if type(exc) is ValueError else '外部MCP连接超时或数据接口不可用，请稍后重试。',
                'scope': scope, 'fetched_at': iso_now()}
    finally:
        _lock.release()
