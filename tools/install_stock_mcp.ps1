$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentPath = Join-Path $projectRoot 'runtime/stock-mcp-venv'
& (Join-Path $projectRoot '.venv/Scripts/python.exe') -m venv $environmentPath
if ($LASTEXITCODE -ne 0) { throw '创建隔离环境失败' }
& (Join-Path $environmentPath 'Scripts/python.exe') -m pip install --index-url https://pypi.org/simple 'stock-data-mcp @ git+https://github.com/stockmcp/stock-data-mcp.git@58f488772eefc6ac2192182fa67517d90922469e' 'fastmcp>=2,<3' 'mcp>=1.20,<2'
if ($LASTEXITCODE -ne 0) { throw 'Stock Data MCP安装失败' }
