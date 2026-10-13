"""`OutboundTransport` —— 对外 event 流的传输边界。

`OutboundStream` 只管「哪些记录该给谁、按什么顺序、推不动了怎么办」。帧怎么真正
到客户端手上（WebSocket / SSE / 别的渠道）不是它的事：绑死一种传输会让背压语义
跟着传输的约束一起改。

有界发送队列在传输侧 —— socket 的写线程是传输的，队列也只能在传输里。但**满了
之后怎么做是策略**，所以传输只报告「装不下」，断不断由 `OutboundStream` 决定。

v1 没有实现 —— 渠道接入方式还没定。接口留在这里是因为背压与游标语义确实与传输
无关，值得先定下来。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Final

from core.protocol.datatype.scope import Scope
from core.utils.encoding import JsonObject


class SendResult(Enum):
    """一帧进发送队列的结果。"""

    #: 排队成功。**不保证已送达网络** —— 送达与否对 core 不可见，core 需要知道的
    #: 只是「已经交给传输了」。
    ACCEPTED: Final = "accepted"
    #: 这个 scope 没有活动连接。订阅可以静默退掉。
    NO_CONNECTION: Final = "no_connection"
    #: 有界队列满了。要立刻返回，由调用方断开这条连接。
    QUEUE_FULL: Final = "queue_full"


class OutboundTransport(ABC):
    """把帧送到已连接客户端的出口。"""

    @abstractmethod
    def send(self, scope: Scope, frame: JsonObject) -> SendResult:
        """把这个 scope 的帧排进发送队列。

        **不能阻塞。** 队列满要立刻返回 `QUEUE_FULL` —— 慢客户端拖住 core 的
        后果比断它严重得多：断开的连接能重连续上（游标还在），阻塞的 runtime 会
        让所有 session 一起卡住。
        """
        ...

    @abstractmethod
    def close(self, scope: Scope, reason: str, message: str = "") -> None:
        """断开这个 scope 的连接。`reason` 取 `CloseReason`，客户端据此退避。"""
        ...


__all__ = ["OutboundTransport", "SendResult"]
