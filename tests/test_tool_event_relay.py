"""`ToolEventRelay` —— 工具终态事件的去处。

两条：直推客户端，进 tool 队列。**不落盘** —— 工具系统不知道这一轮是什么，写出来的
结果和 agent 那一轮对不上。
"""

from __future__ import annotations

import pytest

from core.consumer import ToolEventRelay
from core.protocol.datatype import AgentEvent, InboundKind, Scope
from core.protocol.datatype.message import MessageEvent, MessageEventType
from core.protocol.datatype.tool import ToolEvent, ToolEventType
from core.protocol.interface import AgentEventSink
from core.queue import InMemoryEventQueue

OTHER = Scope(user_id="u1", session_id="s2")


class _Sink(AgentEventSink):
    """记下投递出去的事件。`fail=True` 时每次都抛。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[AgentEvent] = []
        self._fail = fail

    def emit(self, event: AgentEvent) -> None:
        if self._fail:
            raise RuntimeError("投递失败")
        self.events.append(event)


@pytest.fixture
def queue() -> InMemoryEventQueue:
    return InMemoryEventQueue()


@pytest.fixture
def downstream() -> _Sink:
    return _Sink()


@pytest.fixture
def relay(queue: InMemoryEventQueue, downstream: _Sink) -> ToolEventRelay:
    return ToolEventRelay(queue, downstream)


def _terminal(scope: Scope, *, invocation_id: str | None = "inv-1") -> ToolEvent:
    return ToolEvent(
        type=ToolEventType.TERMINAL,
        scope=scope,
        tool_call_id="tc-1",
        invocation_id=invocation_id,
        tool="echo",
        outcome="succeeded",
        payload={
            "tool_call_id": "tc-1",
            "invocation_id": invocation_id or "",
            "outcome": "succeeded",
            "content": "ok",
        },
        idempotency_key="tool.result:inv-1",
        occur_dt="2026-01-01T00:00:00.000000Z",
    )


# --------------------------------------------------------------------------
# 投递
# --------------------------------------------------------------------------


def test_the_client_still_sees_the_terminal_event(
    relay: ToolEventRelay, downstream: _Sink, scope: Scope
) -> None:
    """relay 是加了一层的 sink，不是替代品 —— 客户端那边一个字都不能少。"""
    relay.emit(_terminal(scope))

    assert [e.type for e in downstream.events] == ["tool.terminal"]


def test_delivery_happens_before_enqueueing(
    relay: ToolEventRelay, queue: InMemoryEventQueue, downstream: _Sink, scope: Scope
) -> None:
    """顺序反了，界面上会出现一段先有回答、后有结果的错位：
    客户端要先看见 `tool.terminal` 画上「已完成」，才可能看到紧接着的 `assistant.message`。"""
    order: list[str] = []
    downstream.emit = lambda event: order.append("client")  # type: ignore[method-assign]
    queue.put = lambda scope, item: order.append("queue")  # type: ignore[method-assign]

    relay.emit(_terminal(scope))

    assert order == ["client", "queue"]


def test_a_failing_client_does_not_lose_the_input(
    relay: ToolEventRelay, queue: InMemoryEventQueue, downstream: _Sink, scope: Scope
) -> None:
    """送不出去只是界面少一行 —— 输入照旧进队列，否则这一轮就永远等不到。"""
    downstream._fail = True

    with pytest.raises(RuntimeError):
        relay.emit(_terminal(scope))

    assert len(queue.drain(scope, "tool", limit=64)) == 1


def test_a_failing_client_still_raises(
    relay: ToolEventRelay, downstream: _Sink, scope: Scope
) -> None:
    """relay 自己不吞异常 —— 工具系统要靠它知道这次投递失败了。"""
    downstream._fail = True

    with pytest.raises(RuntimeError):
        relay.emit(_terminal(scope))


# --------------------------------------------------------------------------
# 入队
# --------------------------------------------------------------------------


def test_the_terminal_event_becomes_a_tool_result(
    relay: ToolEventRelay, queue: InMemoryEventQueue, scope: Scope
) -> None:
    relay.emit(_terminal(scope))

    item = queue.drain(scope, "tool", limit=64)[0]
    assert item.kind == InboundKind.TOOL_RESULT
    assert item.source == ToolEventType.TERMINAL
    assert item.source_id == "inv-1"


def test_the_payload_is_carried_untouched(
    relay: ToolEventRelay, queue: InMemoryEventQueue, scope: Scope
) -> None:
    """`tool.result` 的 payload 逐字段对齐 journal 的 schema —— 翻译一次就多一处
    两边字段名可能对不上的地方。"""
    relay.emit(_terminal(scope))

    item = queue.drain(scope, "tool", limit=64)[0]
    assert item.payload == {
        "tool_call_id": "tc-1",
        "invocation_id": "inv-1",
        "outcome": "succeeded",
        "content": "ok",
    }


def test_the_occur_dt_is_carried_over(
    relay: ToolEventRelay, queue: InMemoryEventQueue, scope: Scope
) -> None:
    """等待时限从工具跑完那刻起算，不是从 agent 读到它那刻起算。"""
    relay.emit(_terminal(scope))

    item = queue.drain(scope, "tool", limit=64)[0]
    assert item.occur_dt == "2026-01-01T00:00:00.000000Z"


def test_it_lands_in_the_tool_queue(
    relay: ToolEventRelay, queue: InMemoryEventQueue, scope: Scope
) -> None:
    relay.emit(_terminal(scope))

    assert queue.pending(scope, "tool") == 1
    assert queue.pending(scope, "user") == 0


def test_other_sessions_are_untouched(
    relay: ToolEventRelay, queue: InMemoryEventQueue, scope: Scope
) -> None:
    relay.emit(_terminal(scope))

    assert queue.pending(OTHER, "tool") == 0


def test_nothing_reaches_the_journal_through_here(
    relay: ToolEventRelay, downstream: _Sink, scope: Scope
) -> None:
    """落盘是 `Brain` 读到这一批之后的判断 —— 工具系统写出来的那份不算数。"""
    relay.emit(_terminal(scope))

    assert downstream.events[0].type == "tool.terminal"


# --------------------------------------------------------------------------
# 配不上的那类
# --------------------------------------------------------------------------


def test_an_event_without_an_invocation_id_stays_out_of_the_queue(
    relay: ToolEventRelay, queue: InMemoryEventQueue, downstream: _Sink, scope: Scope
) -> None:
    """配不上任何一次调用，放进队列只会让 brain 读到一条悬空的东西。"""
    relay.emit(_terminal(scope, invocation_id=None))

    assert queue.pending(scope, "tool") == 0
    assert [e.type for e in downstream.events] == ["tool.terminal"]


def test_a_non_terminal_event_is_not_queued(
    relay: ToolEventRelay, queue: InMemoryEventQueue, downstream: _Sink, scope: Scope
) -> None:
    """v1 只有终态一种事件 —— 但这一层是按 type 判的，不是按「反正只有一种」。"""
    event = ToolEvent(
        type="tool.started",
        scope=scope,
        tool_call_id="tc-1",
        invocation_id="inv-1",
        tool="echo",
        outcome="succeeded",
        payload={},
        idempotency_key="k",
        occur_dt="2026-01-01T00:00:00.000000Z",
    )

    relay.emit(event)

    assert queue.pending(scope, "tool") == 0
    assert [e.type for e in downstream.events] == ["tool.started"]


def test_a_non_tool_event_passes_straight_through(
    relay: ToolEventRelay, queue: InMemoryEventQueue, downstream: _Sink, scope: Scope
) -> None:
    event = MessageEvent(
        type=MessageEventType.ASSISTANT_MESSAGE,
        scope=scope,
        payload={"content": "晴"},
        idempotency_key="msg:1",
        occur_dt="2026-01-01T00:00:00.000000Z",
    )

    relay.emit(event)

    assert queue.pending(scope, "tool") == 0
    assert downstream.events == [event]
