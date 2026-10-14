"""`SessionConsumer` —— 每个 session 一条消费线程。

循环只有四步，顺序是这件事的全部：

1. **取光三条队列**，灌进对应的 buffer —— 从这一刻起不会再丢。
2. **把三条 buffer 的全部内容交给 `Brain`**（不是刚取到的那几条：「这一轮齐了没有」
   是跨批次的判断）。
3. `Brain` 说处理完了才 **清 buffer**；说还没到点就按它给的秒数等。
4. 回到 1。

**该等多久、该不该醒，全是 `Brain` 说了算。** 这一层没有「问路由」那一步 —— 醒来之后
batch 里是什么，`Brain` 自己知道。consumer 只管搬运和等。
"""

from __future__ import annotations

import logging
import threading
from typing import Final

from core.protocol.datatype.event import AgentEvent
from core.protocol.datatype.inbound import Batch, Inbound, InboundKind
from core.protocol.datatype.queue import QueueName
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.tool import ToolEvent, ToolEventType
from core.protocol.interface.brain import Brain
from core.protocol.interface.buffer import EventBuffer
from core.protocol.interface.event import AgentEventSink
from core.protocol.interface.queue import EventQueue
from core.runtime import AgentContext
from core.utils.encoding import utc_now_iso

_log = logging.getLogger("oath.consumer")

#: 一次从队列里取多少条。上限，不是页大小 —— 一轮里同时到的结果应当一次取光，
#: 分批取就是给别的输入插队的机会。
_DRAIN_LIMIT: Final = 64

#: 队列空着的时候醒一次看一眼。这不是轮询策略，只是兜底：新输入会 `notify`，
#: 真正靠它醒来的只有重启后捞回来的那批。
_IDLE_WAIT: Final = 5.0

#: 三条 buffer 的读写顺序。`system` 排最后不是因为它不重要，而是因为同一批里
#: 用户输入总是先于控制面指令应答。
_QUEUE_ORDER: Final = (QueueName.USER, QueueName.TOOL, QueueName.SYSTEM)


class ToolEventRelay(AgentEventSink):
    """工具终态事件：直推客户端，然后进 tool 队列。

    **不落盘。** 工具系统不知道这一轮是什么，写出来的 `tool.result` 和 agent 那一轮
    对不上 —— 落盘是 `Brain` 读到这一批之后的判断。

    投递排在入队前面：客户端要先看见 `tool.terminal` 画上「已完成」，才可能看到这一轮
    紧接着的 `assistant.message`。反过来的话界面上会出现一段先有回答、后有结果的错位。
    """

    def __init__(self, queue: EventQueue, downstream: AgentEventSink) -> None:
        self._queue = queue
        self._downstream = downstream

    def emit(self, event: AgentEvent) -> None:
        if (
            isinstance(event, ToolEvent)
            and event.type == ToolEventType.TERMINAL
            and event.invocation_id is not None
        ):
            self._deliver_then_enqueue(event)
            return
        self._downstream.emit(event)

    def _deliver_then_enqueue(self, event: ToolEvent) -> None:
        """先投客户端，再入队。

        反过来的话，消费线程会被入队叫醒、抢在投递完成之前跑完这一步，界面上就出现
        一段先有 `assistant.message`、后有 `tool.terminal` 的错位。

        送不出去也照旧入队：客户端少看一行是它的事，agent 少等一个结果是另一回事。
        """
        try:
            self._downstream.emit(event)
        finally:
            self._queue.put(event.scope, _tool_inbound(event))


class SessionConsumer:
    """一个 session 的消费循环。"""

    def __init__(self, ctx: AgentContext, brain: Brain, buffer: EventBuffer) -> None:
        self._ctx = ctx
        self._brain = brain
        self._buffer = buffer

    def run(self, scope: Scope, stop: threading.Event) -> None:
        """跑到 `stop` 被置位。返回值无意义 —— 崩了的东西都已经落盘或者还在 buffer 里。"""
        # 先捞一次没 clear 的那批，不等新 `put` 来叫醒：进程崩在 step 中途的时候，
        # 队列是空的，只有 buffer 记得有活没干完。
        self._cycle(scope)
        while not stop.is_set():
            self._cycle(scope)

    # -- 一次循环 ----------------------------------------------------------

    def _cycle(self, scope: Scope) -> None:
        self._drain(scope)
        batch = self._batch(scope)
        if batch.empty:
            self._ctx.queue.wait(scope, timeout=_IDLE_WAIT)
            return

        try:
            settled = self._brain.step(scope, batch)
        except Exception:
            # 一条输入处理失败不能让线程死掉 —— 剩下的输入还在队列里，失败的已经由
            # `AgentBrain` 记成 `system.agent_step{error}`。buffer 不清，下一轮重试。
            _log.exception("session %s 的一批输入处理失败", scope)
            self._ctx.queue.wait(scope, timeout=_IDLE_WAIT)
            return

        if settled:
            self._clear(scope)
            return
        delay = self._brain.wakeup_delay(scope, batch)
        self._ctx.queue.wait(scope, timeout=_IDLE_WAIT if delay is None else delay)

    def _drain(self, scope: Scope) -> None:
        """取光三条队列，灌进对应的 buffer。"""
        for queue in _QUEUE_ORDER:
            items = self._ctx.queue.drain(scope, queue, limit=_DRAIN_LIMIT)
            if items:
                self._buffer.put(scope, queue, items)

    def _batch(self, scope: Scope) -> Batch:
        """三条 buffer 现在的全部内容。"""
        return Batch(
            user=self._buffer.snapshot(scope, QueueName.USER),
            tool=self._buffer.snapshot(scope, QueueName.TOOL),
            system=self._buffer.snapshot(scope, QueueName.SYSTEM),
        )

    def _clear(self, scope: Scope) -> None:
        """`Brain` 说处理完了。三条一起清 —— 一批里三样东西是同一件事。"""
        for queue in _QUEUE_ORDER:
            self._buffer.clear(scope, queue)


def _tool_inbound(event: ToolEvent) -> Inbound:
    # 没有 `invocation_id` 的终态事件配不上任何一次调用，放进队列只会让 brain 读到
    # 一条悬空的东西。放行给客户端，不进队列。
    return Inbound(
        kind=InboundKind.TOOL_RESULT,
        source=event.type,
        payload=event.payload,
        source_id=event.invocation_id or "",
        occur_dt=event.occur_dt or utc_now_iso(),
    )


__all__ = ["SessionConsumer", "ToolEventRelay"]
