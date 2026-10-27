# 外部 Stock Data MCP

本项目既保留原有 MCP Server，也通过官方 MCP Python SDK ClientSession / stdio_client 调用独立的 Stock Data MCP 子进程。不是直接绕过 MCP 调 AKShare，也没有把外部数据放入原模型。

来源：https://github.com/stockmcp/stock-data-mcp
固定提交：58f488772eefc6ac2192182fa67517d90922469e（MIT）。注意 PyPI 同名发行版本与此仓库可能不一致，安装脚本固定仓库来源。

Windows 安装：`powershell -File tools/install_stock_mcp.ps1`。独立环境位于 `runtime/stock-mcp-venv`，不改变主项目依赖。新电脑需要重新安装。可选 TUSHARE_TOKEN 仅在服务端环境设置，不在聊天、URL、前端或Git里填写。

所有已登录租户的管理员、经理、普通用户均可查看四个新增菜单。未登录仍不能访问 `/api/stock-observation`，Host/会话校验继续有效。只允许四种页面参数，不能指定进程、URL、工具名称或任意执行代码。

| 菜单 | 固定上游工具 | 参数 |
| --- | --- | --- |
| 个股资金 | stock_fund_flow | symbol：6位A股代码 |
| 涨停强势 | stock_zt_pool | pool_type：涨停/强势，date：交易日，limit：100 |
| 龙虎榜 | stock_lhb_ggtj_sina | days：5/10/30/60，limit：100 |
| 板块资金 | stock_sector_fund_flow_rank | days：今日/5日/10日，cate：行业/概念/地域资金流 |

上游README中涨停工具旧名与实际代码不一致，本客户端握手后发现并检查实际工具。只按用户点击查询，不后台持续抓取。单次55秒超时，全局单任务并发，同参数3分钟公开数据缓存，最多128个缓存项；缓存不含租户资料。进程不向公网监听端口。不传模型API Key、策略和用户身份。子进程错误日志在 runtime/external_stock_mcp/server.stderr.log（不通过网页暴露）。

数据按上游CSV解析，保留代码前导零、列名和原始单位，分页每页10条。查询时间不是行情时间。龙虎榜是最近窗口统计，不是历史单日榜；板块源可能只选头尾各20条，并非全量排名。周末不将旧板块数据标作今日，股池默认最近配置交易日。失败、空结果、格式异常显示不可用，不编造数据或回退旧结果。所有字段用 textContent 展示，外部内容不得作为指令执行。

这些功能不会改写推荐、历史回测、评分或新闻因子配置。社区接口不保证可用性；本地安装免费不代表数据具有免费商业再分发授权。
