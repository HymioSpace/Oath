"""队列与对外 event 流的名字，以及每个 feed 收哪些 type。

三条内部队列（`user` / `tool` / `system`）与对外 event 流（`outbound`）是同一个
东西的四种用法：都是从 journal 上挂一个游标、按 type 过滤、往前推。差别只有
「谁消费」和「收哪些记录」，所以映射表是显式的一张表，不写成四个类。

**队列只装 runtime 的输入。** `assistant.message` 与 `tool.call` 是 runtime 和工具
系统的产物 —— 它们进 journal（对话历史要投影它们），但进的是 outbound，不进任何
一条 runtime 队列。队列回答的是「runtime 现在该干什么」，不是「runtime 干了什么」。

映射不从 type 前缀推：`system.agent_step` 归 system 却是审计记录，前缀规则表达不了
这个区别。
"""

from __future__ import annotations

from typing import Final

from core.protocol.datatype.journal import EventType


class FeedName:
    """feed 的名字。三个 runtime 队列 + 一条对外流。"""

    USER: Final = "user"
    TOOL: Final = "tool"
    SYSTEM: Final = "system"
    OUTBOUND: Final = "outbound"

    ALL: Final = frozenset({USER, TOOL, SYSTEM, OUTBOUND})

    #: runtime 消费的三条。对外的 `outbound` 消费方是客户端，不是 runtime。
    RUNTIME_QUEUES: Final = frozenset({USER, TOOL, SYSTEM})


#: 每条 feed 收哪些 record type。
QUEUE_TYPES: Final[dict[str, frozenset[str]]] = {
    FeedName.USER: frozenset({EventType.USER_MESSAGE}),
    FeedName.TOOL: frozenset({EventType.TOOL_RESULT}),
    FeedName.SYSTEM: EventType.SYSTEM_COMMANDS,
    # 对外流收全部记录：客户端要看到 agent 干了什么，那是 trace 视图的职责，
    # 由客户端自己过滤。服务端替它筛等于替客户端做了决定。
    FeedName.OUTBOUND: EventType.ALL,
}


def belongs_to(feed: str, type_: str) -> bool:
    """这条记录该不该进这个 feed。`feed` 不认识时返回 False，不抛错 ——
    读到一条新 type 的记录不该让整个消费方挂掉。"""
    return type_ in QUEUE_TYPES.get(feed, frozenset())


__all__ = ["QUEUE_TYPES", "FeedName", "belongs_to"]
