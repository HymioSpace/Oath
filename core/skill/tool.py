"""`skill` —— 搜技能、读技能正文。

内置工具，不是扩展：它读的是 core 自己索引的东西，删掉它索引就只剩摘要没有正文。
它的返回值不参与结果截断 —— 模型点名要的东西截掉就没有意义了。
"""

from __future__ import annotations

from typing import Any

from core.protocol.datatype import ToolDefinition, ToolInvocation, ToolResult
from core.protocol.error import ToolExecutionError
from core.protocol.interface.tool import Tool
from core.skill.skill import SkillError
from core.skill.system import SkillSystem

#: 结果不参与截断的工具集合。名字只在这里出现一次。
NAME = "skill"

_SEARCH = "search"
_READ = "read"


class SkillTool(Tool):
    """`search` 按关键词找技能，`read` 取某个技能的完整正文。"""

    def __init__(self, skills: SkillSystem) -> None:
        self._skills = skills
        self._definition = ToolDefinition(
            name=NAME,
            description="查找可用技能，或读取某个技能的完整说明。索引里没有的技能用 search 找。",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [_SEARCH, _READ],
                        "description": "search 按关键词找技能；read 取某个技能的完整说明",
                    },
                    "query": {"type": "string", "description": f"{_SEARCH} 的关键词"},
                    "name": {"type": "string", "description": f"{_READ} 的技能名"},
                },
                "required": ["action"],
            },
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def execute(self, invocation: ToolInvocation) -> ToolResult:
        action = invocation.arguments.get("action")
        if action == _SEARCH:
            return ToolResult(
                content=self._search(
                    self._text(invocation, "query"), channel=invocation.channel
                )
            )
        if action == _READ:
            return ToolResult(
                content=self._read(
                    self._text(invocation, "name"), channel=invocation.channel
                )
            )
        raise ToolExecutionError(f"action 只接受 {_SEARCH} 或 {_READ}: {action!r}")

    def _search(self, query: str, *, channel: str) -> str:
        found = self._skills.find(query, channel=channel)
        if not found:
            return f"没有匹配「{query}」的技能。"
        return "\n".join(f"- {skill.name}: {skill.description}" for skill in found)

    def _read(self, name: str, *, channel: str) -> str:
        try:
            return self._skills.load(name, channel=channel)
        except (KeyError, SkillError) as error:
            raise ToolExecutionError(f"读不了技能 {name}: {error}") from error

    @staticmethod
    def _text(invocation: ToolInvocation, field: str) -> str:
        value: Any = invocation.arguments.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ToolExecutionError(f"这个 action 需要非空的 {field}")
        return value


__all__ = ["NAME", "SkillTool"]
