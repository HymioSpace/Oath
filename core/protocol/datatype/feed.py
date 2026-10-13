"""记录往哪走：三条内部队列，以及对外 event 流。

**队列只装输入，不装输出。** 三条队列（`user` / `tool` / `system`）回答的是「现在该
做什么」，不是「做了什么」。`assistant.message` 与 `tool.call` 是 `Brain` 和工具系统
的产物 —— 它们进 journal（对话历史要投影它们），但进的是对外流，不是任何一条队列。

**对外流不是队列。** oath 内部没有消费方：没有 `wait`、没有「处理完才推进」这回事，
缓冲与重排是客户端自己的事。所以它不在这张映射表里，也没有位置语义 ——
`OutboundStream` 记一个读到的位置只是为了别每次 poll 都重扫整份日志，不是记账。

映射不从 type 前缀推：`system.agent_step` 归 system 却是审计记录，前缀规则表达不了
这个区别。
"""

from __future__ import annotations

from typing import Final

from core.protocol.datatype.journal import EventType


class FeedName:
    """三条内部队列。消费方是 `QueueRunner`。"""

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


#: 对外流推哪些 record type：**客户端没发过的那部分。**
#:
#: 客户端发过的不推 —— 回声对它没有信息量，它自己就有。真的需要一条记录的游标，
#: `ack` 帧回得更直接。
#:
#: `tool.result` 同时在 `tool` 队列与这里：agent 要它作为输入，客户端要它显示工具状态。
#: 两边要的不是同一件事，各走各的通路。
#:
#: 审批事件还没进 journal，所以不在这个集合里。`permission.requested` 该进这里
#: （客户端要拿它渲染审批界面），`permission.resolved` 该进 `tool` 队列（agent 要知道
#: 结果）—— 那一步做完时两个都要加。
CLIENT_FACING: Final = frozenset(
    {
        EventType.ASSISTANT_MESSAGE,
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
        EventType.SYSTEM_AGENT_STEP,
    }
)


def belongs_to(feed: str, type_: str) -> bool:
    """这条记录该不该进这个 feed。`feed` 不认识时返回 False，不抛错 ——
    读到一条新 type 的记录不该让整个消费方挂掉。"""
    return type_ in QUEUE_TYPES.get(feed, frozenset())


__all__ = ["CLIENT_FACING", "QUEUE_TYPES", "FeedName", "belongs_to"]
