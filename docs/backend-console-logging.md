# 后端结构化控制台日志

服务控制台每行输出一个 JSON 对象，中文保留原文。并发线程的日志按整行写入，及时刷新输出。

每条新日志包含 `pid`（进程编号），启动成功事件为 `web_service_listening`。Windows监听端口采用独占绑定，禁止两个实例共用8001；端口被占用时明确报错，不再自动换端口，绑定失败也不启动定时器。请只保留一个IDE或终端服务实例。前端请求日志输出在真正接收请求的进程控制台，而不会自动转发到另一个控制台。

所有网页GET/POST（包括未登录、Host/CSRF拒绝、权限拒绝）记录 `http_request_started` 与 `http_request_finished`：包含路径、安全查询字段、HTTP状态、耗时、用户/租户。返回 `X-Request-ID`；未知异常记录脱敏堆栈并返回可追踪的请求编号。日志不记录HTTP请求体、Cookie、授权头和完整响应体。

已登录页面每次同源fetch发送请求编号，浏览器控制台打印路径/状态/耗时；网络失败、JS错误、未捕获Promise错误通过 `/api/client-events` 上报 `frontend_event`。上报沿用登录、Host、Origin及CSRF校验，前端每分钟最多20条、后端每用户每分钟最多30条。只接收元信息，不接收任意错误正文、堆栈或凭据；网络失败与后端使用 `related_request_id` 关联。页面本身未能加载时无法上报前端错误，但后端请求日志仍存在。

`tool_started/tool_result/tool_failed/tool_finished` 覆盖公共工具，记录参数（脱敏）、返回列表数量及耗时。`upstream_request_started/upstream_request_failed/upstream_request_finished` 覆盖HttpClient外部数据访问，记录域名、路径、参数名、状态、字节数、耗时，不记录查询参数值和数据正文。已有Agent及模型日志保持。

服务启动时另外落盘 `runtime/logs/backend.jsonl`，单文件10MB，保留5份滚动备份。该文件包含已有模型调试请求/响应，属于敏感业务日志，仅限本机授权人员读取，不通过网页提供。

后台服务实时查看：

```powershell
Get-Content .\runtime\logs\backend.jsonl -Tail 50 -Wait
```

- `agent_event`：采集 Agent 开始、完成、重试等事件；完成时包含 `agent`、`records`、`duration_ms` 和 `message`。
- `agent_started` / `agent_finished` / `agent_failed` / `agent_skipped`：五角色链路状态，包含 `run_id`、角色、耗时和数据摘要；数据缺失不会标记为成功。
- `llm_request`：实际提交给模型的 JSON 请求，包括模型、`messages` 或 `input` 等提示词字段；每个传输尝试单独记录。
- `llm_response`：模型返回的 JSON 数据、可用的 `usage` 和请求耗时。接口不返回用量时为 `null`，不估算 token 数量。
- `answer_succeeded` / `answer_transport_failed` / `answer_fallback`：成功、传输失败与本地回答回退。配合 `llm_call_finished` 查看整个调用耗时。

一次模型调用的日志使用相同 `request_id`。Agent 内发起的解释还带有 `run_id` 和角色；行情与新闻并行采集也会传递运行上下文。

API Key、授权头、已知密钥值和 URL 中常见凭据会脱敏。提示词、系统快照与模型回答属于完整调试内容，会出现在本机控制台；分享日志前应检查业务数据。已有 LLM 事件文件只保存摘要事件，不额外保存完整请求响应。

在终端启动可直接查看：

```powershell
.\.venv\Scripts\python.exe -u -m a_stock_agents web --port 8001 --no-refresh-on-start
```

如果服务通过后台方式启动，标准输出会进入启动命令指定的日志文件。接口失败时只会记录真实失败，不会生成模拟的模型回答来充当成功日志。
