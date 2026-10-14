"""三条内部队列：输入从哪来。

**队列只装输入。** 三条队列（`user` / `tool` / `system`）回答的是「现在该做什么」，不是
「做了什么」。producer 侧压根没有产物 —— `assistant.message` 与 `tool.call` 是 `Brain`
写出来的，它们不排队。

映射是闭合的：`Inbound` 的三种 kind 每一种都有固定的队列，所以这张表没有「返回 None」
那一支。
"""

from __future__ import annotations

from typing import Final

from core.protocol.datatype.inbound import InboundKind


class QueueName:
    """三条内部队列，各自由 UserConsumer / ToolConsumer / SystemConsumer 消费。"""

    USER: Final = "user"
    TOOL: Final = "tool"
    SYSTEM: Final = "system"

    ALL: Final = frozenset({USER, TOOL, SYSTEM})


#: kind 各进哪条队列。
QUEUE_OF_KIND: Final[dict[str, str]] = {
    InboundKind.USER: QueueName.USER,
    InboundKind.TOOL_RESULT: QueueName.TOOL,
    InboundKind.INVOKE: QueueName.SYSTEM,
}


def queue_of(kind: str) -> str:
    """这件事该进哪条队列。"""
    try:
        return QUEUE_OF_KIND[kind]
    except KeyError:
        raise ValueError(f"未知的 InboundKind: {kind!r}") from None


__all__ = ["QUEUE_OF_KIND", "QueueName", "queue_of"]
