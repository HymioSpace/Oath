"""provider 无关的模型调用数据。

请求与响应都是纯数据，不带任何一家的字段名。谁家的形状由 `ModelProvider` 的实现
翻译（见 `core/llm/`）—— core 里的每一个类都不知道 `messages` 在外面叫什么。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from core.protocol.datatype.message import Message
from core.protocol.datatype.tool import ToolDefinition
from core.utils.encoding import JsonObject


class LlmFinishReason:
    """模型为什么停下来。

    只有 `TOOL_CALLS` 与 `LlmResponse` 的 `tool_calls` 非空是同一个意思，两处都要看 ——
    有的厂家给出这个 reason 但把工具塞在别处，有的反过来。
    """

    STOP: Final = "stop"
    LENGTH: Final = "length"
    TOOL_CALLS: Final = "tool_calls"
    CONTENT_FILTER: Final = "content_filter"

    ALL: Final = frozenset({STOP, LENGTH, TOOL_CALLS, CONTENT_FILTER})


@dataclass(frozen=True, slots=True)
class LlmToolCall:
    """模型要调的一个工具。

    `tool_call_id` 是模型给的，原样透传 —— assistant 轮里的 `tool_calls[i].id` 与 tool
    结果消息里的 `tool_call_id` 必须逐字节相同，任何 provider 都会拿这对值配对。
    """

    tool: str
    arguments: JsonObject
    tool_call_id: str


@dataclass(frozen=True, slots=True)
class LlmRequest:
    """一次模型请求。"""

    messages: list[Message]
    tools: tuple[ToolDefinition, ...] = ()
    model: str = ""
    max_output_tokens: int = 2048
    temperature: float | None = None
    extra_params: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LlmResponse:
    """模型的一次回应。

    `content` 与 `tool_calls` 可以同时非空：有的模型一边说话一边要求调工具，两样都要
    落进 assistant 那一轮。
    """

    content: str = ""
    reasoning: str | None = None
    tool_calls: tuple[LlmToolCall, ...] = ()
    finish_reason: str = LlmFinishReason.STOP
    usage: JsonObject = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        """这一轮有没有工具要派发。"""
        return bool(self.tool_calls)


__all__ = ["LlmFinishReason", "LlmRequest", "LlmResponse", "LlmToolCall"]
