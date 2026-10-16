"""三条输入队列：输入从哪来。

**队列只装输入。** 三条队列（`user` / `tool` / `system`）回答的是「现在该做什么」，不是
「做了什么」。producer 侧压根没有产物 —— `assistant.message` 与 `tool.call` 是 `Brain`
写出来的，它们不排队。

队列名就是 `Inbound.kind`，两个名字指同一个东西。`user` / `tool` / `system` 三条，闭合成
一张等式，没有「这件事不知道该进哪条队列」这种状态。
"""

from __future__ import annotations

from typing import Final

from core.protocol.error import UnknownQueueError


class QueueName:
    """三条输入队列，各自由 UserConsumer / ToolConsumer / SystemConsumer 消费。"""

    USER: Final = "user"
    TOOL: Final = "tool"
    SYSTEM: Final = "system"

    ALL: Final = frozenset({USER, TOOL, SYSTEM})


def check_queue(name: str) -> str:
    """校验队列名，返回原值。

    名字不认识是调用方的 bug，当场暴露。与「读到一条不认识 type 的记录」不同：那种只是
    没命中，静默跳过就够。
    """
    if name not in QueueName.ALL:
        raise UnknownQueueError(f"未知的队列名: {name!r}")
    return name


__all__ = ["QueueName", "check_queue"]
