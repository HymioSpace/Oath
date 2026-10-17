"""`OutboundTransport` —— 对外投递的传输边界。

`AgentEventSink` 的实现拿到一条事件，套成帧，从这里送出去。帧怎么真正到客户端手上
（WebSocket / SSE / 别的渠道）不是 core 的事：绑死一种传输会让背压语义跟着传输的约束
一起改。

有界发送队列在传输侧 —— socket 的写线程是传输的，队列也只能在传输里。但**满了之后
怎么做是策略**，所以传输只报告「装不下」，断不断由 sink 的实现决定。

v1 没有实现 —— 渠道接入方式还没定。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Final

from core.protocol.datatype.user import User
from core.utils.encoding import JsonObject


class SendResult(Enum):
    """一帧进发送队列的结果。"""

    #: 排队成功。**不保证已送达网络** —— 送达与否对 core 不可见，core 需要知道的
    #: 只是「已经交给传输了」。
    ACCEPTED: Final = "accepted"
    #: 这个 user 没有活动连接。事件就此丢掉，没有别的地方可以补。
    NO_CONNECTION: Final = "no_connection"
    #: 有界队列满了。要立刻返回，由调用方断开这条连接。
    QUEUE_FULL: Final = "queue_full"


class OutboundTransport(ABC):
    """把帧送到已连接客户端的出口。"""

    @abstractmethod
    def send(self, user: User, frame: JsonObject) -> SendResult:
        """把这个 user 的帧排进发送队列。

        **不能阻塞。** 队列满要立刻返回 `QUEUE_FULL` —— 慢客户端拖住 core 的后果比
        断它严重得多。
        """
        ...

    @abstractmethod
    def close(self, user: User, reason: str, message: str = "") -> None:
        """断开这个 user 的连接。`reason` 取 `CloseReason`，客户端据此退避。"""
        ...


__all__ = ["OutboundTransport", "SendResult"]
