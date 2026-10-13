"""`ToolResultRouter` —— 工具的终态事件怎么回到对话里。"""

from __future__ import annotations

from core.intake import RESULT_KEY_PREFIX, ToolResultRouter
from core.protocol.datatype import Cursor, CursorKind, EventType, Scope
from core.protocol.datatype.event import AgentEvent
from core.protocol.datatype.message import MessageEvent, MessageEventType
from core.protocol.datatype.tool import ToolEvent, ToolEventType
from core.protocol.interface import AgentEventSink, Journal
from core.utils.encoding import JsonObject, JsonValue


class _Downstream(AgentEventSink):
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)


class _Queue:
    """只记下入队的记录 —— 顺序与真队列一致，行为不必复刻。"""

    def __init__(self) -> None:
        self.queued: list[object] = []

    def put(self, scope: Scope, record: object) -> None:
        self.queued.append(record)


def _terminal(
    scope: Scope,
    *,
    invocation_id: str | None = "i-1",
    tool_call_id: str | None = "t-1",
    outcome: str = "succeeded",
    **payload: JsonValue,
) -> ToolEvent:
    body: JsonObject = {
        "tool_call_id": tool_call_id or "",
        "invocation_id": invocation_id or "",
        "outcome": outcome,
        **payload,
    }
    return ToolEvent(
        type=ToolEventType.TERMINAL,
        scope=scope,
        tool_call_id=tool_call_id,
        invocation_id=invocation_id,
        tool="echo",
        outcome=outcome,
        payload=body,
        idempotency_key="k",
        occur_dt="2026-10-13T00:00:00Z",
    )


def _router(journal: Journal) -> tuple[ToolResultRouter, _Queue, _Downstream]:
    queue = _Queue()
    downstream = _Downstream()
    return ToolResultRouter(journal, queue, downstream), queue, downstream  # type: ignore[arg-type]


def _types(journal: Journal, scope: Scope) -> list[str]:
    return [r.type for r in journal.read_range(scope, Cursor(CursorKind.LINE, 0))]


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------


def test_a_terminal_event_is_journalled(journal: Journal, scope: Scope) -> None:
    router, _, _ = _router(journal)

    router.emit(_terminal(scope))

    assert _types(journal, scope) == [EventType.TOOL_RESULT]


def test_the_outcome_is_recorded(journal: Journal, scope: Scope) -> None:
    """下一次 build 靠它决定这条 tool 消息算不算已终结。"""
    router, _, _ = _router(journal)

    router.emit(_terminal(scope, outcome="failed", content=None, error="boom"))

    record = next(iter(journal.read_range(scope, Cursor(CursorKind.LINE, 0))))
    assert record.payload["outcome"] == "failed"
    assert record.payload["error"] == "boom"


def test_both_identifiers_are_recorded(journal: Journal, scope: Scope) -> None:
    """少了任一个，投影都对不上：结果就悬在那条 tool 槽位旁边。"""
    router, _, _ = _router(journal)

    router.emit(_terminal(scope))

    record = next(iter(journal.read_range(scope, Cursor(CursorKind.LINE, 0))))
    assert record.payload["tool_call_id"] == "t-1"
    assert record.payload["invocation_id"] == "i-1"


def test_the_invocation_id_is_the_idempotency_key(journal: Journal, scope: Scope) -> None:
    """同一个调用重发终态不会写出两条结果。"""
    router, _, _ = _router(journal)

    router.emit(_terminal(scope))
    router.emit(_terminal(scope))

    assert _types(journal, scope) == [EventType.TOOL_RESULT]


def test_a_result_without_an_invocation_id_is_not_recorded(journal: Journal, scope: Scope) -> None:
    """配不上任何一次调用 —— 落进去只是让投影读到一条悬空的 tool 消息。"""
    router, queue, _ = _router(journal)

    router.emit(_terminal(scope, invocation_id=None))

    assert _types(journal, scope) == []
    assert queue.queued == []


# --------------------------------------------------------------------------
# 入队
# --------------------------------------------------------------------------


def test_the_result_goes_back_on_the_queue(journal: Journal, scope: Scope) -> None:
    """它是一次新的输入 —— agent 得再跑一轮才知道工具跑成了什么。"""
    router, queue, _ = _router(journal)

    router.emit(_terminal(scope))

    assert [r.type for r in queue.queued] == [EventType.TOOL_RESULT]  # type: ignore[attr-defined]


def test_the_queued_record_matches_what_was_written(journal: Journal, scope: Scope) -> None:
    router, queue, _ = _router(journal)

    router.emit(_terminal(scope))

    queued = queue.queued[0]
    written = next(iter(journal.read_range(scope, Cursor(CursorKind.LINE, 0))))
    assert queued.payload == written.payload  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# 投递
# --------------------------------------------------------------------------


def test_the_event_still_reaches_the_client(journal: Journal, scope: Scope) -> None:
    """落盘不是终点 —— 客户端要看见工具跑完了。"""
    router, _, downstream = _router(journal)

    router.emit(_terminal(scope))

    assert downstream.events == [_terminal(scope)]


def test_other_events_are_only_delivered(journal: Journal, scope: Scope) -> None:
    """`assistant.message` 由 brain 自己落盘，这里再写一遍就是两条记录。"""
    router, queue, downstream = _router(journal)
    message = MessageEvent(
        type=MessageEventType.ASSISTANT_MESSAGE,
        scope=scope,
        payload={"content": "晴", "reasoning": None},
        idempotency_key="k",
        occur_dt="2026-10-13T00:00:00Z",
    )

    router.emit(message)

    assert downstream.events == [message]
    assert _types(journal, scope) == []
    assert queue.queued == []


def test_the_prefix_matches_the_tool_systems(journal: Journal, scope: Scope) -> None:
    """两边的幂等键前缀必须一致，否则同一次结果会被算成两次。"""
    from core.protocol.datatype.tool import RESULT_KEY_PREFIX as TOOL_PREFIX

    router, _, _ = _router(journal)

    router.emit(_terminal(scope))

    record = next(iter(journal.read_range(scope, Cursor(CursorKind.LINE, 0))))
    assert record.idempotency_key == f"{RESULT_KEY_PREFIX}i-1" == f"{TOOL_PREFIX}i-1"
