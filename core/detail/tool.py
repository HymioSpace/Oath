"""`detail_read` —— 读回被截断的工具结果全文。

内置工具，不是扩展：它读的是 core 自己挪走的东西，删掉它截断就成了单向删除。
"""

from __future__ import annotations

from core.protocol.datatype import ToolDefinition, ToolInvocation, ToolResult
from core.protocol.error import ToolExecutionError
from core.protocol.interface.detail import DetailStore
from core.protocol.interface.tool import Tool

#: 结果不参与截断的工具集合。名字只在这里出现一次。
NAME = "detail_read"


class DetailReadTool(Tool):
    """按 `detail_key` 取全文。key 不存在和 user 不匹配都报取不到。"""

    def __init__(self, store: DetailStore) -> None:
        self._store = store
        self._definition = ToolDefinition(
            name=NAME,
            description="读取某个被截断的工具结果的完整内容。detail_key 来自结果里的截断说明。",
            input_schema={
                "type": "object",
                "properties": {"detail_key": {"type": "string", "description": "截断说明里的 key"}},
                "required": ["detail_key"],
            },
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def execute(self, invocation: ToolInvocation) -> ToolResult:
        key = invocation.arguments.get("detail_key")
        if not isinstance(key, str) or not key:
            raise ToolExecutionError("detail_key 必须是文本")
        content = self._store.get(invocation.user, key)
        if content is None:
            raise ToolExecutionError(f"取不到 detail_key={key}")
        return ToolResult(content)


__all__ = ["NAME", "DetailReadTool"]
