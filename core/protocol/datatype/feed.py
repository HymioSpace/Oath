"""记录往哪走：三条内部队列。

**队列只装输入，不装输出。** 三条队列（`user` / `tool` / `system`）回答的是「现在该
做什么」，不是「做了什么」。`assistant.message` 与 `tool.call` 是 `Brain` 和工具系统
的产物 —— 它们进 journal（对话历史要投影它们），但进的是任何一条队列都没有。

推给客户端不在这张表里。那是活的投递：产生事件的一方调 `AgentEventSink.emit` 直推，
不走 journal、不读位置。客户端自己就是它已渲染内容的存储方。

映射不从 type 前缀推：`system.agent_step` 归 system 却是审计记录，前缀规则表达不了
这个区别。
"""

from __future__ import annotations

from typing import Final

from core.protocol.datatype.journal import EventType


class FeedName:
    """三条内部队列。消费方是 `Intake`。"""

    USER: Final = "user"
    TOOL: Final = "tool"
    SYSTEM: Final = "system"

    ALL: Final = frozenset({USER, TOOL, SYSTEM})


#: 每条队列收哪些 record type。
QUEUE_TYPES: Final[dict[str, frozenset[str]]] = {
    FeedName.USER: frozenset({EventType.USER_MESSAGE}),
    FeedName.TOOL: frozenset({EventType.TOOL_RESULT}),
    FeedName.SYSTEM: EventType.SYSTEM_COMMANDS,
}


def belongs_to(feed: str, type_: str) -> bool:
    """这条记录该不该进这个 feed。`feed` 不认识时返回 False，不抛错 ——
    读到一条新 type 的记录不该让整个消费方挂掉。"""
    return type_ in QUEUE_TYPES.get(feed, frozenset())


__all__ = ["QUEUE_TYPES", "FeedName", "belongs_to"]
