# 标准日志格式

项目是 Python 应用，不引入 Java Log4j 运行库，采用 Log4j PatternLayout 风格：

`yyyy-MM-dd HH:mm:ss.SSS LEVEL [thread] astock.module [pid=... request_id=... run_id=...] - event | key=value`

统一使用北京时间。INFO 为正常运行，WARN 为重试、降级和4xx，ERROR 为异常、失败和5xx。控制台展示请求、响应、工具参数和结果、状态码、耗时及关联编号。请求/响应/堆栈等字段最多显示8000字符，其他字段2000字符，截断会标注；JSON事件保留已采集的完整字段。公共上游原始正文只采样前8000字节并注明是否截断，避免把整个全市场响应反复写入日志。多行字段会转义，敏感信息在输出前统一脱敏。

- `runtime/logs/backend.log`：同控制台格式，错误堆栈保留在文件中。
- `runtime/logs/backend.jsonl`：完整结构化事件，用于定位请求、模型响应及自动处理。
- 两类文件均按10MB轮转，分别保留5份备份，不改变既有文件内容。
- 正常状态轮询只写文件，不输出到控制台；异常仍输出。
- 更新格式后需要重新启动原来的后端进程，不要另开重复服务。
