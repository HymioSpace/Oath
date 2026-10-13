"""`WsEventSink` —— 把 core 的事件变成往下一帧。

**投递失败不当异常抛给 core。** 事件已经落盘，对话不会缺，缺的是客户端那一眼。
抛错会让 `AgentBrain` 以为这次 emit 失败了，而它对「客户端看不见」这件事无能为力 ——
所以这里的动作是：队列满就断开那条连接，事件丢掉，不阻塞 core。

工具系统的终态事件走同一个 sink，但那边失败会记 `outcome_unknown` —— 那不是
矛盾：工具结果丢了 agent 就不知道结果，而一条消息没送到只是界面少了一行。
"""

from __future__ import annotations

from core.protocol.datatype.event import AgentEvent
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import CloseReason, EventFrame
from core.protocol.interface.event import AgentEventSink
from core.protocol.interface.transport import OutboundTransport, SendResult


class WsEventSink(AgentEventSink):
    """事件 → `event` 帧 → 传输。"""

    def __init__(self, transport: OutboundTransport) -> None:
        self._transport = transport

    def emit(self, event: AgentEvent) -> None:
        scope: Scope = event.scope
        result = self._transport.send(scope, EventFrame.of(event).to_json())
        if result is SendResult.QUEUE_FULL:
            self._transport.close(scope, CloseReason.SLOW_CONSUMER)
        # NO_CONNECTION 什么都不用做：事件就此丢掉，本来也没有别的地方可以补。


__all__ = ["WsEventSink"]
