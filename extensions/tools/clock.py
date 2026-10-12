"""`current_time` —— 返回当前 UTC 时刻。

参考实现，最小形状：给模型看的定义 + 一个把内容映射成 `ToolResult` 的 `execute`。
模型面向的 `description` 用英文 —— 它是随请求发给 provider 的字面量，不是给人读的
文档；仓库里的注释与文档字符串才是中文。
"""

from __future__ import annotations

from typing import Final

from core.protocol.datatype import ToolDefinition, ToolInvocation, ToolResult
from core.protocol.interface import Tool
from core.utils.encoding import JsonObject, utc_now_iso

_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


class ClockTool(Tool):
    """当前时间。模型判断「今天」「三天后」这类问题时的唯一可信来源 ——
    模型自身对当前日期的认知是不可靠的，也不该依赖它。"""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="current_time",
            description=(
                "Return the current time as an RFC 3339 UTC timestamp. "
                "Use it whenever the answer depends on what 'now' is: today's date, "
                "deadlines, ages, relative scheduling. It takes no arguments."
            ),
            input_schema=_SCHEMA,
        )

    def execute(self, invocation: ToolInvocation) -> ToolResult:
        return ToolResult(content=utc_now_iso())


__all__ = ["ClockTool"]
