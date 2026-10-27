# Harness 分层运行架构

## 原则

可信运行时持有计划、工具权限、阶段验收和恢复预算。模型只能提交计划执行意见或受限恢复动作，不修改策略、权限、密钥或代码。正式选股、影子研究、盘中辅助分析保留原业务校验，不用模型“自述成功”替代真实执行结果。

## 分层

```text
入口层：web.py / cli.py / scheduler.py / mcp_server.py
应用层：supervisor.py / workflow.py / shadow.py / intraday.py
业务层：agents/ / selection.py / models.py / recommendation_store.py
Harness：
  planning/tool.py   版本化计划工具：步骤、依赖、允许工具、验收要求
  runtime.py         统一阶段执行、检查点复用、输入匹配、并发锁与验收
  context.py         上下文指纹、大小检查；字符数不冒充Token数
  gateway.py         工具调用边界、资料截止时间、工具/模型调用档案
  policy.py          结构化错误分类、可配置重试策略
  persistence.py     SQLite任务、阶段检查点、事件与调用存储
  locking.py         单机进程互斥，不依赖业务调度器
  control/master.py  主模型受限协调、运行状态及修复展示
  adapters/llm.py    原生Function Calling服务商协议适配
  evaluation.py     不调用模型的执行轨迹一致性验收
基础设施：HttpClient / Milvus / SQLite / 文件归档 / 统一日志
```

这是单机可信运行底座，不引入分布式队列。应用层保留任务业务，Harness不计算选股分数。根目录原业务模块没有大规模搬迁，以免影响已有CLI、数据路径和历史记录。

## 执行计划是一个工具

`get_execution_plan(task_type)` 支持 `formal`、`shadow`、`intraday`，通过 AgentTools、原生工具schema和本系统MCP stdio可读取。计划返回 `version`、`steps`、`depends_on`、`allowed_tools`、`acceptance`、`effect`。

- 正式：数据获取 → 基本面 → 技术面 → 风控 → 正式决策。
- 影子：多维检索与财务准备 → 基本面 → 技术研究 → 风险复核 → 独立决策。
- 盘中：校验当前行情 → 辅助分析 → 保存该时段记录。

计划是程序维护的可信模板，不接受任意shell或模型生成代码，不因读取工具而启动任务。正式和影子图的阶段顺序均取自此工具；盘中同样按这份计划检查阶段依赖。原有角色级工具限制和业务验收继续保留。

## 持久化与恢复

新增 `runtime/harness/runs.db`：

- `runs`：任务类型、版本、输入指纹、最终状态、累计恢复次数。
- `checkpoints`：阶段状态、输入指纹、输出、执行次数。
- `events`：阶段状态变化记录。
- `tool_calls`：工具调用参数、结果及失败类别，凭据脱敏。
- `model_calls`：模型发送前状态、服务商请求编号、实际返回用量和不确定结果。

工具和模型调用表按实际使用创建。已有账号、正式推荐、影子实验、盘中记录和主控事件库不删除、不重写。Harness只做新的运行账本。

成功阶段只在相同运行编号、输入和计划版本下复用。进程锁避免同阶段并发执行。正式发布阶段如曾开始但无法确认结果，暂停人工核对，绝不盲目重放。已经完成的正式结果可以读取检查点，不会重复发布。恢复正式任务限制同日、相同策略和准入配置；原行情时效和锁定校验仍然有效。

影子阶段检查点额外保存已检索证据、候选覆盖、前序结论和已报告用量；同一个实验实例编号重新进入执行时可以复用成功阶段。**本次CLI恢复入口只开放正式任务**，没有自动重跑旧影子报告的操作入口，也没有将历史报告伪造为Harness新任务。盘中仍遵守日期/时段/代码唯一，不自动重放已认领时段，不跨时段补造行情。

模型请求先登记，再发送；收到响应即保存服务商编号和usage，即使结论解析失败也保留用量。已发送后无有效回执的请求标记结果待确认，阻止自动重放。没有实现服务商账单对账或人工批准重放接口，因此这类任务需核对日志后另建有明确授权的新任务，不应直接改数据库。

## 恢复配置

`config/settings.json` 的 `harness.recovery` 支持：

```json
{
  "max_worker_retries": 2,
  "max_run_recoveries": 6,
  "backoff_seconds": 1.0,
  "retry_http_statuses": [408, 429, 500, 502, 503, 504]
}
```

未指定的Worker次数、退避时间继承原 `supervisor` 配置。HTTP白名单只允许收窄，不能加入401、403或业务错误；未知SourceBlockedError不再默认视为临时网络问题。正式Worker最多首次加两次重试；运行内财务传输恢复和主控恢复共享累计恢复预算。主模型不可用时使用明确标记的程序策略。

`harness.max_daily_pipeline_attempts` 默认为3，阻止定时器每隔一段时间无休止创建新流水线。达到上限进入 `needs_review`，不影响管理员明确发起的新任务。未恢复旧的30,000 Token限制；模型/工具次数、超时与已有上下文大小约束继续有效。

## 使用

```powershell
.venv/Scripts/python.exe -m a_stock_agents.cli plan formal
.venv/Scripts/python.exe -m a_stock_agents.cli plan shadow
.venv/Scripts/python.exe -m a_stock_agents.cli plan intraday
.venv/Scripts/python.exe -m a_stock_agents.cli audit <run_id>
.venv/Scripts/python.exe -m a_stock_agents.cli resume <formal_run_id>
```

管理员点击顶部“主Agent”弹框，可查看实际计划和最近任务状态。普通用户没有计划详情或产物读取权限；原Host、登录、租户、CSRF和策略保密逻辑不变。数据库不应通过静态文件服务对外开放。自动轮询仍不输出控制台日志，但持久文件保留。

## 清理与兼容

- `master_agent.py` 已迁移到 `harness/control/master.py`，内部引用与测试同步更新，不留双份实现。
- `shadow_llm.py` 已迁移到 `harness/adapters/llm.py`，继续兼容千问及现有兼容接口。
- 删除未引用的 `_legacy_send`，实际响应仍经过 `AccessControl._send`。
- 删除已不可见的旧“自动任务”状态DOM和更新逻辑；顶部推荐倒计时组件保留。
- 移除没有测试用例、导入即依赖外部API的 `tests/test.py`；原文件先备份到 `runtime/backups/harness-migration/legacy-manual-api-test.py`，可恢复，内容不打印。
- 旧验证截图、历史报告和用户业务数据不是废弃代码，本次不删除。

## 验收与边界

离线测试覆盖计划参数校验、计划副本不可污染、依赖阻断、失败不缓存成成功、重启检查点复用、输入变更拒绝、未知发布不重放、工具越权/未来数据拦截、调用档案脱敏、模型已接收但校验失败的用量留存、模型结果不确定阻断、累计预算、调度熔断及完整轨迹检查。原业务回归继续验证正式结果和当日锁定未改变。

`audit` 检查的是轨迹结构一致性，不等于投资效果评测；模型语义质量、前瞻收益及真实接口稳定性仍需单独的固定快照评测与前瞻观察。不自动调参、不优化线上收益目标、不自动部署代码、不终止未知进程。Python进程崩溃仍需外部服务管理器，当前没有分布式调度、远端任务取消或无人审批代码修复。

部署时只需重启当前后端，不启动重复实例。新增SQLite库按任务惰性创建，无需迁移原业务库或新增云组件。
