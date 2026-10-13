"""`ToolResultRouter` —— 工具的终态事件怎么回到对话里。

工具系统的终态事件是从工作线程里发出来的，谁都不负责把它变成一条记录。结果不落盘，
下一次 `ContextManager.build` 就看不见这个调用发生过：agent 不知道工具跑了、也不知道
跑成了什么，等于这次调用从对话里消失了。

所以这条链是 **落盘 → 入队 → 投递**，顺序固定。入队在投递之前：事件到了客户端、
界面已经画了「正在调 echo」，而 journal 里还没有 —— 那一刻断线重连，这段就查不到了。

只处理 `tool.terminal`。`assistant.message` 与 `tool.call` 由 `AgentBrain` 自己落盘
（它就是生产者，知道自己写了什么），这里再写一遍就是两条记录。
"""

from __future__ import annotations

from core.protocol.datatype import Cursor
from core.protocol.datatype.event import AgentEvent
from core.protocol.datatype.journal import EventType, Record
from core.protocol.datatype.tool import ToolEvent, ToolEventType
from core.protocol.interface.event import AgentEventSink
from core.protocol.interface.journal import Journal
from core.protocol.interface.queue import EventQueue
from core.utils.encoding import utc_now_iso

#: `tool.result` 幂等键的前缀。
RESULT_KEY_PREFIX = "tool.result:"


class ToolResultRouter(AgentEventSink):
    """落盘 + 入队，然后原样递给下游。"""

    def __init__(self, journal: Journal, queue: EventQueue, downstream: AgentEventSink) -> None:
        self._journal = journal
        self._queue = queue
        self._downstream = downstream

    def emit(self, event: AgentEvent) -> None:
        if isinstance(event, ToolEvent) and event.type == ToolEventType.TERMINAL:
            self._record(event)
        self._downstream.emit(event)

    def _record(self, event: ToolEvent) -> None:
        if event.invocation_id is None:
            # 没有调用 id 的终态事件配不上任何一次调用 —— 落进去也只是让
            # 投影读到一条悬空的 tool 消息。放行，不制造垃圾记录。
            return
        # `ToolEvent.payload` 已经逐字段对齐 `tool.result` 的 payload schema，
        # 原样 append，不翻译。
        key = f"{RESULT_KEY_PREFIX}{event.invocation_id}"
        cursor = self._journal.append(
            event.scope,
            EventType.TOOL_RESULT,
            event.payload,
            payload_schema_version=1,
            idempotency_key=key,
        ).cursor
        self._queue.put(event.scope, _queued(cursor, event, key))


def _queued(cursor: Cursor, event: ToolEvent, key: str) -> Record:
    """队列里放的那条记录。

    payload 与幂等键是刚写进去的那一份，不从 journal 读回来 —— 队列只读 `type`
    与 `payload`，再扫一遍文件换不来第二份真相。
    """
    return Record.of(
        cursor,
        {
            "user_id": event.scope.user_id,
            "session_id": event.scope.session_id,
            "type": EventType.TOOL_RESULT,
            "payload": event.payload,
            "payload_schema_version": 1,
            "idempotency_key": key,
            "occur_dt": event.occur_dt,
            "commit_dt": utc_now_iso(),
        },
    )


__all__ = ["RESULT_KEY_PREFIX", "ToolResultRouter"]
