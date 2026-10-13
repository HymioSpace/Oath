"""`Message` —— LLM 对话历史里的一条消息。

模型适配层、agent runtime、客户端的会话视图都要用它，journal 只是它的一个
生产者。字段用 `role` / `content` 这套通用名，不带任何一家 provider 的字段名。

`MessageEvent` 是这条消息往外的形态：落盘归 journal，投递归 `AgentEventSink`，
两件事分开。消息事件没有工具，所以基类那三个工具字段取 `None`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from core.protocol.datatype.event import AgentEvent
from core.utils.encoding import JsonObject


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    content: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转成 provider 请求里的形状。`extra` 里的键原样带出去。"""
        return {"role": self.role, "content": self.content, **self.extra}


@dataclass(frozen=True, slots=True)
class ToolCallRef:
    """对话历史里的一个 tool call 引用。

    形状是**扁平的** `{id, name, arguments}` —— 不带任何一家把工具包在
    `function` 对象里的写法。那种嵌套是 OpenAI 的 wire 形状，翻译是
    `ModelProvider` 的活：`Message.extra` 与 journal payload 里存的都是这份
    扁平形态，所以两者永远逐字节相同，投影不需要认任何一家的字段。

    `arguments` 是已经解析好的对象，不是 JSON 字符串：解析在哪一层发生是
    provider 的自由，落进 core 的东西一律是解析好的。
    """

    id: str
    name: str
    arguments: JsonObject

    def to_dict(self) -> JsonObject:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


class MessageEventType:
    """消息事件的类型。与 journal 的 `EventType` 用同一套字符串。

    `TOOL_CALL` 也从这里走：它也是模型说的话的一部分，客户端要靠它显示「正在调什么」。

    `DELTA` 是唯一**不落盘**的一个：它是一段增量文本，收到就往界面上追加，重放时靠
    完整的 `ASSISTANT_MESSAGE` 兜底。落盘它等于让 journal 里出现半句话。
    """

    USER_MESSAGE: Final = "user.message"
    ASSISTANT_MESSAGE: Final = "assistant.message"
    TOOL_CALL: Final = "tool.call"
    DELTA: Final = "assistant.delta"

    ALL: Final = frozenset({USER_MESSAGE, ASSISTANT_MESSAGE, TOOL_CALL, DELTA})


@dataclass(frozen=True, slots=True)
class MessageEvent(AgentEvent):
    """一条对话消息，往外送。

    `payload` 逐字段对齐落盘记录的 payload，所以投递到 journal 时可以直接 append。
    `tool_call_id` / `invocation_id` 只有 `tool.call` 事件才带 —— 那两个标识是模型调用
    才有的东西。
    """

    def to_dict(self) -> JsonObject:
        return {
            "type": self.type,
            "user_id": self.scope.user_id,
            "session_id": self.scope.session_id,
            "payload": self.payload,
            "idempotency_key": self.idempotency_key,
            "occur_dt": self.occur_dt,
        }


__all__ = ["Message", "MessageEvent", "MessageEventType", "ToolCallRef"]
