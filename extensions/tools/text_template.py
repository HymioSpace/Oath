"""`render_template` —— 按 `{{name}}` 占位符渲染模板。

参考实现，最小形状：入参经 `input_schema` 校验后怎么用、失败怎么抛
`ToolExecutionError`。没有任何 I/O，不碰外部系统。
"""

from __future__ import annotations

import re
from typing import Final

from core.protocol.datatype import ToolDefinition, ToolInvocation, ToolResult
from core.protocol.error import ToolExecutionError
from core.protocol.interface import Tool
from core.utils.encoding import JsonObject, canonical_json

_PLACEHOLDER: Final = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "template": {
            "type": "string",
            "description": "The template text. Use {{name}} placeholders to insert values.",
        },
        "values": {
            "type": "object",
            "description": "Values keyed by placeholder name. Every placeholder must be present.",
        },
    },
    "required": ["template", "values"],
    "additionalProperties": False,
}


class TextTemplateTool(Tool):
    """把固定格式的文本按变量填好。拼提示词片段、消息、报告骨架时用得上。"""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="render_template",
            description=(
                "Render a text template by substituting {{placeholder}} markers with "
                "values from a dictionary. Every placeholder must have a matching key; "
                "a missing one is an error rather than an empty string, so a rendering "
                "bug is never mistaken for an intentionally blank field."
            ),
            input_schema=_SCHEMA,
        )

    def execute(self, invocation: ToolInvocation) -> ToolResult:
        template = _require_str(invocation.arguments, "template")
        values = _require_object(invocation.arguments, "values")

        def substitute(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in values:
                # 缺失就报错，不渲染成空串 —— 静默填空会让一个渲染 bug
                # 看起来像一个故意留白的字段。
                raise ToolExecutionError(f"模板占位符没有对应取值: {{{{{key}}}}}")
            value = values[key]
            # 渲染结果只能是文本。非字符串取值走 canonical JSON，而不是 `str()` ——
            # 后者给列表套上引号，给布尔写成 True。
            return value if isinstance(value, str) else canonical_json(value)

        return ToolResult(content=_PLACEHOLDER.sub(substitute, template))


def _require_str(arguments: JsonObject, field: str) -> str:
    value = arguments.get(field)
    # schema 已经保证过类型；这里是防御 ——
    # 工具也可能被直接调用，不经过派发时的 schema 校验。
    if not isinstance(value, str):
        raise ToolExecutionError(f"{field} 必须是字符串: {value!r}")
    return value


def _require_object(arguments: JsonObject, field: str) -> JsonObject:
    value = arguments.get(field)
    if not isinstance(value, dict):
        raise ToolExecutionError(f"{field} 必须是 JSON 对象: {value!r}")
    return value


__all__ = ["TextTemplateTool"]
