"""`echo` —— 原样回显一段文本。

给模型看的定义用英文：它是随请求发给 provider 的字面量，不是给人读的文档。
"""

from __future__ import annotations

from typing import Final

from core.protocol.datatype import ToolDefinition, ToolInvocation, ToolResult
from core.protocol.interface import Tool
from core.utils.encoding import JsonObject

_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "The text to echo back.",
        }
    },
    "required": ["text"],
    "additionalProperties": False,
}


class EchoTool(Tool):
    """回显。

    存在的理由是验证工具链路：调用 → 派发 → 执行 → 终态事件 → 回填对话历史，
    一圈下来看到的就是模型给的原文，中间没有加工。"""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="echo",
            description=(
                "Echo a piece of text back unchanged. "
                "Use it to verify a tool round-trip; it has no side effects."
            ),
            input_schema=_SCHEMA,
        )

    def execute(self, invocation: ToolInvocation) -> ToolResult:
        return ToolResult(content=str(invocation.arguments.get("text", "")))


__all__ = ["EchoTool"]
