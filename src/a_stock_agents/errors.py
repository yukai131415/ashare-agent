class AgentError(RuntimeError):
    """可分类、可由总控 Agent 处理的业务异常。"""


class SourceBlockedError(AgentError):
    """数据源拒绝请求或触发反爬。"""


class SchemaChangedError(AgentError):
    """上游响应成功，但字段结构不再符合预期。"""


class DataUnavailableError(AgentError):
    """当前没有足够数据完成任务。"""


class StateCorruptedError(AgentError):
    """本地状态文件损坏。"""

