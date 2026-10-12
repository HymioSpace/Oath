"""工具系统与 journal 的对接：终态事件直接落成 `tool.result` 记录。

跨模块契约的验证点只有一条：**事件 payload 恰好是合法的 `tool.result` payload，
所以投递方不需要翻译**。这里的 sink 就是 journal，链路是真的：

    tool.call 记录 → 派发 → 终态事件 → tool.result 记录 → recover() 投影成 tool 消息
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from core.journal.impl import FileJournal
from core.protocol.datatype import (
    AgentState,
    Cursor,
    EventType,
    Record,
    Scope,
    ToolCallRequest,
    ToolEvent,
    ToolOutcome,
)
from core.protocol.datatype.permission import PermissionEventType, PermissionVerdict
from core.protocol.interface import ToolEventSink
from core.tool import ToolRegistry
from core.tool.impl import ThreadedToolSystem
from extensions.tools import ClockTool
from tests._support.tool_fixtures import (
    EchoTool,
    RecordingPermissionSink,
    VerdictPolicy,
    permission_manager,
    wait_terminal,
)

SCOPE = Scope(user_id="u1", session_id="s1")
BEGINNING = Cursor(kind="line", value=0)


class JournalSink(ToolEventSink):
    """把终态事件直接 append 进 journal。字段一一对应，没有翻译层。"""

    def __init__(self, journal: FileJournal) -> None:
        self._journal = journal

    def emit(self, event: ToolEvent) -> None:
        self._journal.append(
            SCOPE,
            EventType.TOOL_RESULT,
            event.payload,
            payload_schema_version=1,
            idempotency_key=event.idempotency_key,
            occur_dt=event.occur_dt,
        )


@pytest.fixture
def journal(root: Path) -> Iterator[FileJournal]:
    with FileJournal(root) as j:
        yield j


def build(journal: FileJournal) -> ThreadedToolSystem:
    registry = ToolRegistry()
    registry.register(ClockTool(), timeout_seconds=5.0)
    return ThreadedToolSystem(registry, JournalSink(journal), permission_manager())


def commit_snapshot(journal: FileJournal) -> None:
    """提交一份覆盖到日志末尾的快照 —— agent runtime 每步都会做的事。

    没有快照时 `recover()` 的水位线是日志开头，`messages` 自然为空：快照水位线
    与对话历史对齐，这条对齐不是本 feature 定的，但它决定了这里必须先提交快照。
    """
    records = _records(journal)
    assert records
    journal.commit_state(AgentState.covering(SCOPE, records[-1].cursor, {"step": "done"}))


def test_a_terminal_event_becomes_a_tool_result_record(journal: FileJournal) -> None:
    # 先落 tool.call —— 对话历史里模型留下的空位从这里开始。
    journal.append(
        SCOPE,
        EventType.TOOL_CALL,
        {
            "tool_call_id": "toolu_1",
            "invocation_id": "inv-1",
            "tool": "current_time",
            "arguments": {},
        },
        payload_schema_version=1,
        idempotency_key="call:inv-1",
    )

    system = build(journal)
    try:
        batch = system.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-inv-1",
                    tool="current_time",
                    arguments={},
                    invocation_id="inv-1",
                )
            ],
        )
        execution = wait_terminal(system, SCOPE, "inv-1")
        assert batch.handles[0].invocation_id == "inv-1"
        assert execution.outcome == ToolOutcome.SUCCEEDED

        commit_snapshot(journal)
        recovery = journal.recover(SCOPE)
        tool_messages = [m for m in recovery.messages if m.role == "tool"]
        assert len(tool_messages) == 1
        assert execution.result is not None
        assert tool_messages[0].content == execution.result.content
        assert tool_messages[0].extra["outcome"] == ToolOutcome.SUCCEEDED
        # 投影用模型给的 tool_call_id，不是 Oath 的调用 id。
        assert tool_messages[0].extra["tool_call_id"] == "toolu_1"
    finally:
        system.shutdown(wait=False)


def test_a_failed_call_also_fills_the_slot(journal: FileJournal) -> None:
    """当场被拒的调用没有执行记录，但**仍然发终态事件** —— 否则这里会是一个
    `unresolved` 的空 tool 消息，交给任何 provider 都是坏的数组。"""
    journal.append(
        SCOPE,
        EventType.TOOL_CALL,
        {
            "tool_call_id": "toolu_2",
            "invocation_id": "inv-2",
            "tool": "not_registered",
            "arguments": {},
        },
        payload_schema_version=1,
        idempotency_key="call:inv-2",
    )

    system = build(journal)
    try:
        batch = system.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-inv-2",
                    tool="not_registered",
                    arguments={},
                    invocation_id="inv-2",
                )
            ],
        )
        assert batch.handles[0].state == "rejected"

        commit_snapshot(journal)
        recovery = journal.recover(SCOPE)
        tool_messages = [m for m in recovery.messages if m.role == "tool"]
        assert len(tool_messages) == 1
        assert tool_messages[0].extra["outcome"] == ToolOutcome.FAILED
        assert "unresolved" not in tool_messages[0].extra
    finally:
        system.shutdown(wait=False)


def test_the_same_terminal_event_appends_once(journal: FileJournal) -> None:
    """事件带幂等键，所以同一次执行的终态事件投递两次在 journal 侧是幂等的。"""
    system = build(journal)
    try:
        system.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id="tc-i3", tool="current_time", arguments={}, invocation_id="i3"
                )
            ],
        )
        wait_terminal(system, SCOPE, "i3")
        event = next(iter(_records(journal)))
        journal.append(
            SCOPE,
            EventType.TOOL_RESULT,
            event.payload,
            payload_schema_version=1,
            idempotency_key=event.idempotency_key,
            occur_dt=event.occur_dt,
        )
        assert [r.type for r in _records(journal)] == [EventType.TOOL_RESULT]
    finally:
        system.shutdown(wait=False)


def test_a_batch_leaves_no_unfilled_tool_call(journal: FileJournal) -> None:
    """一批里三种判定各来一个，模型留下的三个槽位必须都被填上。

    LLM 的 message 数组里每个 tool call 都要配一条 tool result，缺一条这个数组
    交给任何 provider 都是坏的。所以「等批准」也必须是一条终态结果 —— 它就是
    历史本身，审批回来之后不会再有第二条去覆盖它。
    """
    calls = [
        ("toolu_ok", "inv-ok", "echo_ok"),
        ("toolu_no", "inv-no", "echo_no"),
        ("toolu_wait", "inv-wait", "echo_wait"),
    ]
    for tool_call_id, invocation_id, tool in calls:
        journal.append(
            SCOPE,
            EventType.TOOL_CALL,
            {
                "tool_call_id": tool_call_id,
                "invocation_id": invocation_id,
                "tool": tool,
                "arguments": {},
            },
            payload_schema_version=1,
            idempotency_key=f"call:{invocation_id}",
        )

    perm_sink = RecordingPermissionSink()
    manager = permission_manager(
        VerdictPolicy({"echo_no": PermissionVerdict.DENY, "echo_wait": PermissionVerdict.ASK}),
        sink=perm_sink,
    )
    registry = ToolRegistry()
    for _, _, tool in calls:
        registry.register(EchoTool(tool), timeout_seconds=5.0)
    system = ThreadedToolSystem(registry, JournalSink(journal), manager)
    try:
        batch = system.execute(
            SCOPE,
            [
                ToolCallRequest(
                    tool_call_id=f"tc-{invocation_id}",
                    tool=tool,
                    arguments={},
                    invocation_id=invocation_id,
                )
                for _, invocation_id, tool in calls
            ],
        )
        assert [h.state for h in batch.handles] == ["accepted", "rejected", "pending_approval"]
        wait_terminal(system, SCOPE, "inv-ok")

        commit_snapshot(journal)
        recovery = journal.recover(SCOPE)
        tool_messages = [m for m in recovery.messages if m.role == "tool"]

        assert len(tool_messages) == 3
        assert "unresolved" not in [k for m in tool_messages for k in m.extra]
        # 投影用模型给的那个 id，与 assistant 轮里的 tool_calls 逐字节对齐。
        assert [m.extra["tool_call_id"] for m in tool_messages] == [
            "toolu_ok",
            "toolu_no",
            "toolu_wait",
        ]
        assert [m.extra["outcome"] for m in tool_messages] == [
            ToolOutcome.SUCCEEDED,
            ToolOutcome.FAILED,
            ToolOutcome.AWAITING_APPROVAL,
        ]

        requested = perm_sink.of_type(PermissionEventType.REQUESTED)
        assert [event.invocation_id for event in requested] == ["inv-wait"]
    finally:
        system.shutdown(wait=False)
        manager.shutdown(wait=False)


def _records(journal: FileJournal) -> list[Record]:
    return list(journal.read_range(SCOPE, BEGINNING))
