"""三条内部队列：输入从哪来。

**队列只装输入。** 三条队列（`user` / `tool` / `system`）回答的是「现在该做什么」，不是
「做了什么」。`assistant.message` 与 `tool.call` 是 `Brain` 和工具系统的产物 —— 它们进
journal（对话历史要投影它们），但不在下面的表里，所以不进任何队列。

映射不从 type 前缀推：产物干脆不列，比给它们编一个「哪条都不进」的队列更不容易读错。
"""

from __future__ import annotations

from typing import Final

from core.protocol.datatype.journal import EventType


class QueueName:
    """三条内部队列。消费方是 `Intake`。"""

    USER: Final = "user"
    TOOL: Final = "tool"
    SYSTEM: Final = "system"

    ALL: Final = frozenset({USER, TOOL, SYSTEM})


#: 输入型记录各进哪条队列。产物不在表里 —— 它们只进 journal，不排队。
INPUT_QUEUE: Final[dict[str, str]] = {
    EventType.USER_MESSAGE: QueueName.USER,
    EventType.TOOL_RESULT: QueueName.TOOL,
    EventType.SYSTEM_CANCEL: QueueName.SYSTEM,
    EventType.SYSTEM_INTERRUPT: QueueName.SYSTEM,
}


def queue_of(type_: str) -> str | None:
    """这条记录该进哪条队列。产物返回 None —— 它们不是输入。"""
    return INPUT_QUEUE.get(type_)


__all__ = ["INPUT_QUEUE", "QueueName", "queue_of"]
