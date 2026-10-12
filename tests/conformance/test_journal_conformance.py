"""Journal 契约测试。

这些测试跑在 `Journal` **接口**上，不碰实现内部细节（文件路径、索引结构、
fd 管理）。将来加 MySQL 实现，同一组测试应该原样跑通——那时候把它们参数化到
多个实现上，而不是复制一份。

对应 `spec/feature-journal.md` 的「验收与验证」。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from core.protocol.datatype import (
    AgentState,
    Cursor,
    CursorKind,
    EventType,
    Record,
    Scope,
    ToolOutcome,
)
from core.protocol.error import (
    CorruptRecordError,
    CursorError,
    IdempotencyKeyConflict,
    JournalError,
    PayloadSchemaError,
    PayloadVersionError,
    ScopeError,
    ValidationError,
)
from core.protocol.interface import Journal
from core.utils.encoding import JsonObject

# --------------------------------------------------------------------------
# 夹具级工具
# --------------------------------------------------------------------------


def snapshot_through(journal: Journal, scope: Scope, cursor: Cursor) -> None:
    """提交一份覆盖到 `cursor` 的快照。"""
    journal.commit_state(scope, AgentState.covering(cursor, {"ok": True}), payload_schema_version=1)


def full_history(journal: Journal, scope: Scope) -> list[str]:
    """恢复后调用方真正看到的完整对话历史。

    `messages` 与 `state` 对齐同一水位线，快照之后的记录在 `pending` 里，
    由调用方 fold 之后再投影。完整历史 = 两者相加。
    """
    from core.journal import project_messages

    recovery = journal.recover(scope)
    return [m.content for m in recovery.messages + project_messages(recovery.pending)]


def user_msg(journal: Journal, scope: Scope, n: int, content: str | None = None) -> Cursor:
    result = journal.append(
        scope,
        EventType.USER_MESSAGE,
        {"content": content or f"第 {n} 条"},
        payload_schema_version=1,
        idempotency_key=f"u-{n}",
    )
    return result.cursor


def assistant_msg(
    journal: Journal, scope: Scope, key: str, *, reasoning: str | None = "想了想"
) -> Cursor:
    result = journal.append(
        scope,
        EventType.ASSISTANT_MESSAGE,
        {"content": "好的", "reasoning": reasoning},
        payload_schema_version=1,
        idempotency_key=key,
    )
    return result.cursor


def tool_round(
    journal: Journal,
    scope: Scope,
    invocation_id: str,
    *,
    outcome: str = ToolOutcome.SUCCEEDED,
    attempt: int = 1,
    content: str = "结果",
) -> tuple[Cursor, Cursor]:
    call = journal.append(
        scope,
        EventType.TOOL_CALL,
        {"invocation_id": invocation_id, "tool": "search", "arguments": {"q": "x"}},
        payload_schema_version=1,
        idempotency_key=f"call-{invocation_id}",
    )
    result = journal.append(
        scope,
        EventType.TOOL_RESULT,
        {
            "invocation_id": invocation_id,
            "attempt_id": f"{invocation_id}#{attempt}",
            "outcome": outcome,
            "content": content,
        },
        payload_schema_version=1,
        idempotency_key=f"result-{invocation_id}-{attempt}",
    )
    return call.cursor, result.cursor


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------


def test_append_returns_increasing_cursors(journal: Journal, scope: Scope) -> None:
    cursors = [user_msg(journal, scope, i) for i in range(5)]
    assert [c.value for c in cursors] == [1, 2, 3, 4, 5]
    assert all(c.kind == CursorKind.LINE for c in cursors)


def test_recover_without_snapshot_returns_empty_state(journal: Journal, scope: Scope) -> None:
    user_msg(journal, scope, 1)
    recovery = journal.recover(scope)
    assert recovery.state == AgentState.covering(Cursor(CursorKind.LINE, 0))
    # 没有快照就没有水位线，messages 必然为空，全部记录落在 pending。
    # 这不是缺陷：state 与 messages 对齐同一 cursor，宁可都退回起点，
    # 也不能给出一个「state 是空的但模型记得刚才说过什么」的组合。
    assert recovery.messages == []
    assert len(recovery.pending) == 1
    assert full_history(journal, scope) == ["第 1 条"]


def test_messages_follow_append_order_not_timestamps(journal: Journal, scope: Scope) -> None:
    journal.append(
        scope,
        EventType.USER_MESSAGE,
        {"content": "第一条"},
        payload_schema_version=1,
        idempotency_key="a",
        occur_dt="2026-01-01T00:00:00Z",
    )
    journal.append(
        scope,
        EventType.USER_MESSAGE,
        {"content": "第二条"},
        payload_schema_version=1,
        idempotency_key="b",
        # occur_dt 故意倒着给：顺序由游标决定，不由时间戳决定。
        occur_dt="1999-01-01T00:00:00Z",
    )
    assert full_history(journal, scope) == ["第一条", "第二条"]


def test_snapshot_cuts_state_and_messages_at_same_cursor(journal: Journal, scope: Scope) -> None:
    first = user_msg(journal, scope, 1)
    assistant_msg(journal, scope, "a1")
    journal.commit_state(scope, AgentState.covering(first, {"turn": 1}), payload_schema_version=1)

    user_msg(journal, scope, 2)

    recovery = journal.recover(scope)
    assert recovery.state == AgentState.covering(first, {"turn": 1})
    assert recovery.state.cursor_end == first
    # state 与 messages 对齐同一水位线：第 2 条只在 pending 里，不进 messages。
    assert [m.content for m in recovery.messages] == ["第 1 条"]
    assert [r.payload["content"] for r in recovery.pending] == ["好的", "第 2 条"]


def test_read_range_is_exclusive_of_the_cursor(journal: Journal, scope: Scope) -> None:
    for i in range(4):
        user_msg(journal, scope, i)
    second = Cursor(CursorKind.LINE, 2)
    records = list(journal.read_range(scope, second))
    assert [r.payload["content"] for r in records] == ["第 2 条", "第 3 条"]


def test_read_range_respects_limit(journal: Journal, scope: Scope) -> None:
    for i in range(5):
        user_msg(journal, scope, i)
    records = list(journal.read_range(scope, Cursor(CursorKind.LINE, 0), limit=2))
    assert len(records) == 2


def test_scopes_are_isolated(journal: Journal, scope: Scope) -> None:
    other = Scope(user_id="u1", session_id="s2")
    snapshot_through(journal, scope, user_msg(journal, scope, 1, "甲会话"))
    snapshot_through(journal, other, user_msg(journal, other, 1, "乙会话"))
    assert [m.content for m in journal.recover(scope).messages] == ["甲会话"]
    assert [m.content for m in journal.recover(other).messages] == ["乙会话"]


# --------------------------------------------------------------------------
# 幂等
# --------------------------------------------------------------------------


def test_replaying_the_same_call_does_not_append(journal: Journal, scope: Scope) -> None:
    payload: JsonObject = {"content": "同一句"}
    first = journal.append(
        scope, EventType.USER_MESSAGE, payload, payload_schema_version=1, idempotency_key="k"
    )
    second = journal.append(
        scope, EventType.USER_MESSAGE, payload, payload_schema_version=1, idempotency_key="k"
    )
    assert first.idempotent_hit is False
    assert second.idempotent_hit is True
    assert second.cursor == first.cursor
    assert len(list(journal.read_range(scope, Cursor(CursorKind.LINE, 0)))) == 1


def test_same_key_different_payload_conflicts(journal: Journal, scope: Scope) -> None:
    journal.append(
        scope,
        EventType.USER_MESSAGE,
        {"content": "甲"},
        payload_schema_version=1,
        idempotency_key="k",
    )
    with pytest.raises(IdempotencyKeyConflict):
        journal.append(
            scope,
            EventType.USER_MESSAGE,
            {"content": "乙"},
            payload_schema_version=1,
            idempotency_key="k",
        )


def test_idempotency_survives_restart(root: Path, scope: Scope) -> None:
    from core.journal.impl import FileJournal

    def make() -> FileJournal:
        return FileJournal(root)

    with make() as first:
        first.append(
            scope,
            EventType.USER_MESSAGE,
            {"content": "只应有一条"},
            payload_schema_version=1,
            idempotency_key="k",
        )
    with make() as second:
        result = second.append(
            scope,
            EventType.USER_MESSAGE,
            {"content": "只应有一条"},
            payload_schema_version=1,
            idempotency_key="k",
        )
        assert result.idempotent_hit is True
        assert len(list(second.read_range(scope, Cursor(CursorKind.LINE, 0)))) == 1


def test_idempotency_key_is_scoped_not_global(journal: Journal) -> None:
    a = Scope(user_id="u1", session_id="s1")
    b = Scope(user_id="u1", session_id="s2")
    for s in (a, b):
        result = journal.append(
            s,
            EventType.USER_MESSAGE,
            {"content": "同名键"},
            payload_schema_version=1,
            idempotency_key="shared",
        )
        assert result.idempotent_hit is False


# --------------------------------------------------------------------------
# messages 投影
# --------------------------------------------------------------------------


def test_tool_call_and_result_project_to_one_tool_message(journal: Journal, scope: Scope) -> None:
    assistant_msg(journal, scope, "a")
    _, result = tool_round(journal, scope, "inv-1")
    snapshot_through(journal, scope, result)
    messages = journal.recover(scope).messages
    assert [m.role for m in messages] == ["assistant", "tool"]
    assert messages[1].extra["tool_call_id"] == "inv-1"


def test_tool_retry_keeps_only_the_final_attempt(journal: Journal, scope: Scope) -> None:
    assistant_msg(journal, scope, "a")
    tool_round(journal, scope, "inv-1", outcome=ToolOutcome.FAILED, attempt=1, content="第一次失败")
    _, result = tool_round(
        journal, scope, "inv-1", outcome=ToolOutcome.SUCCEEDED, attempt=2, content="第二次成功"
    )
    snapshot_through(journal, scope, result)
    messages = journal.recover(scope).messages
    tool_messages = [m for m in messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0].content == "第二次成功"


def test_unresolved_tool_call_is_surfaced_not_dropped(journal: Journal, scope: Scope) -> None:
    assistant_msg(journal, scope, "a")
    journal.append(
        scope,
        EventType.TOOL_CALL,
        {"invocation_id": "inv-9", "tool": "search", "arguments": {}},
        payload_schema_version=1,
        idempotency_key="call-9",
    )
    from core.journal import project_messages

    recovery = journal.recover(scope)
    messages = recovery.messages + project_messages(recovery.pending)
    # 丢掉它会产出一个「有 tool_calls 却没有对应结果」的数组，
    # 交给任何 provider 都是坏的。
    assert messages[-1].role == "tool"
    assert messages[-1].extra["unresolved"] is True


def test_reasoning_is_persisted_and_projected(journal: Journal, scope: Scope) -> None:
    cursor = assistant_msg(journal, scope, "a", reasoning="先看日志再看代码")
    snapshot_through(journal, scope, cursor)
    messages = journal.recover(scope).messages
    assert messages[0].extra["reasoning"] == "先看日志再看代码"


def test_system_records_do_not_enter_messages(journal: Journal, scope: Scope) -> None:
    user_msg(journal, scope, 1)
    journal.append(
        scope,
        "system.agent_step",
        {"step_id": "s1", "phase": "begin"},
        payload_schema_version=1,
        idempotency_key="step-1",
    )
    snapshot_through(journal, scope, Cursor(CursorKind.LINE, 2))
    recovery = journal.recover(scope)
    assert [m.role for m in recovery.messages] == ["user"]
    assert recovery.pending == []
    # 记录确实在日志里，只是不进对话历史。
    assert len(list(journal.read_range(scope, Cursor(CursorKind.LINE, 0)))) == 2


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../../etc", "a/b", "", "x" * 65, "a b", "用户1"])
def test_scope_rejects_unsafe_ids(bad: str) -> None:

    with pytest.raises(ScopeError):
        Scope(user_id=bad, session_id="s1")
    with pytest.raises(ScopeError):
        Scope(user_id="u1", session_id=bad)


def test_unknown_payload_version_raises(journal: Journal, scope: Scope) -> None:
    with pytest.raises(PayloadVersionError):
        journal.append(
            scope,
            EventType.USER_MESSAGE,
            {"content": "x"},
            payload_schema_version=99,
            idempotency_key="k",
        )


def test_missing_required_payload_field_raises(journal: Journal, scope: Scope) -> None:
    with pytest.raises(PayloadSchemaError):
        journal.append(
            scope,
            EventType.TOOL_RESULT,
            {"invocation_id": "x", "attempt_id": "y"},  # 缺 outcome
            payload_schema_version=1,
            idempotency_key="k",
        )


def test_invalid_outcome_raises(journal: Journal, scope: Scope) -> None:
    with pytest.raises(PayloadSchemaError):
        journal.append(
            scope,
            EventType.TOOL_RESULT,
            {"invocation_id": "x", "attempt_id": "y", "outcome": "maybe"},
            payload_schema_version=1,
            idempotency_key="k",
        )


def test_unknown_cursor_kind_is_rejected(journal: Journal, scope: Scope) -> None:
    with pytest.raises(CursorError):
        journal.commit_state(
            scope,
            AgentState.covering(Cursor(CursorKind.ID, 3), {"a": 1}),  # MySQL 的游标，JSONL 不认
            payload_schema_version=1,
        )
    with pytest.raises(CursorError):
        list(journal.read_range(scope, Cursor(CursorKind.ID, 1)))


def test_cursor_start_after_cursor_end_is_rejected(journal: Journal, scope: Scope) -> None:
    with pytest.raises(CursorError):
        journal.commit_state(
            scope,
            AgentState.covering(
                Cursor(CursorKind.LINE, 2), {"a": 1}, cursor_start=Cursor(CursorKind.LINE, 5)
            ),
            payload_schema_version=1,
        )


def test_unknown_event_type_is_rejected(journal: Journal, scope: Scope) -> None:

    with pytest.raises(ValidationError):
        journal.append(
            scope,
            "not.a.known.type",
            {},
            payload_schema_version=1,
            idempotency_key="k",
        )


def test_every_journal_failure_is_a_journal_error() -> None:
    """`except JournalError` 接住全部失败 —— 这是契约，消费方靠它兜底。"""
    for exc in (
        ScopeError,
        PayloadVersionError,
        PayloadSchemaError,
        CursorError,
        IdempotencyKeyConflict,
        CorruptRecordError,
    ):
        assert issubclass(exc, JournalError), exc.__name__


def test_state_survives_repeated_commits(journal: Journal, scope: Scope) -> None:
    cursor = user_msg(journal, scope, 1)
    for turn in (1, 2, 3):
        journal.commit_state(
            scope, AgentState.covering(cursor, {"turn": turn}), payload_schema_version=1
        )
    assert journal.recover(scope).state == AgentState.covering(cursor, {"turn": 3})


def test_close_is_idempotent(root: Path, scope: Scope) -> None:
    from core.journal.impl import FileJournal

    j = FileJournal(root)
    user_msg(j, scope, 1)
    j.close()
    j.close()


def test_read_range_on_empty_log_yields_nothing(journal: Journal, scope: Scope) -> None:
    records: Iterator[Record] = journal.read_range(scope, Cursor(CursorKind.LINE, 0))
    assert list(records) == []
