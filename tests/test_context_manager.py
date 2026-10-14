"""`ContextManager` —— journal → 一次模型请求要的东西。"""

from __future__ import annotations

from core.context import ContextManager
from core.protocol.datatype import Cursor, CursorKind
from core.protocol.datatype.agent_state import AgentState
from core.protocol.datatype.config import AgentConfig
from core.protocol.datatype.journal import EventType
from core.protocol.datatype.message import Message
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.tool import ToolDefinition
from core.protocol.interface import Journal
from core.utils.encoding import JsonObject

# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


def _append(journal: Journal, scope: Scope, type_: str, payload: JsonObject, key: str) -> None:
    journal.append(scope, type_, payload, payload_schema_version=1, idempotency_key=key)


# --------------------------------------------------------------------------
# 投影
# --------------------------------------------------------------------------


def test_build_projects_the_whole_history(journal: Journal, scope: Scope) -> None:
    _append(journal, scope, EventType.USER_MESSAGE, {"content": "北京天气"}, "u1")
    _append(journal, scope, EventType.ASSISTANT_MESSAGE, {"content": "晴", "reasoning": None}, "a1")

    context = ContextManager(journal, AgentConfig()).build(scope, ())

    assert [(m.role, m.content) for m in context.messages] == [
        ("user", "北京天气"),
        ("assistant", "晴"),
    ]


def test_build_includes_records_after_the_snapshot(journal: Journal, scope: Scope) -> None:
    """快照之后的那段也是这一轮对话的一部分 —— 只用 `Recovery.messages` 会看不见刚发生的。"""
    _append(journal, scope, EventType.USER_MESSAGE, {"content": "第一句"}, "u1")
    journal.commit_state(AgentState.covering(scope, Cursor(CursorKind.LINE, 1), {"seen": 1}))
    _append(journal, scope, EventType.USER_MESSAGE, {"content": "第二句"}, "u2")

    context = ContextManager(journal, AgentConfig()).build(scope, ())

    assert [m.content for m in context.messages] == ["第一句", "第二句"]


def test_build_carries_a_tool_call_slot(journal: Journal, scope: Scope) -> None:
    """assistant 带的 tool_calls 与后来的 tool.result 要对得上 —— 那靠 journal 投影。"""
    _append(journal, scope, EventType.ASSISTANT_MESSAGE, {"content": "", "reasoning": None}, "a1")
    _append(
        journal,
        scope,
        EventType.TOOL_CALL,
        {"tool_call_id": "t-1", "invocation_id": "i-1", "tool": "echo", "arguments": {}},
        "c1",
    )
    _append(
        journal,
        scope,
        EventType.TOOL_RESULT,
        {"tool_call_id": "t-1", "invocation_id": "i-1", "outcome": "succeeded", "content": "ok"},
        "r1",
    )

    messages = ContextManager(journal, AgentConfig()).build(scope, ()).messages

    assert [m.role for m in messages] == ["assistant", "tool"]
    assert messages[1].extra["tool_call_id"] == "t-1"
    assert messages[1].content == "ok"


# --------------------------------------------------------------------------
# system prompt
# --------------------------------------------------------------------------


def test_system_prompt_comes_first(journal: Journal, scope: Scope) -> None:
    _append(journal, scope, EventType.USER_MESSAGE, {"content": "hi"}, "u1")

    context = ContextManager(journal, AgentConfig(system_prompt="你是助手")).build(scope, ())

    assert context.messages[0] == Message(role="system", content="你是助手")


def test_build_does_not_commit_a_snapshot(journal: Journal, scope: Scope) -> None:
    """快照是加速恢复的，不是正确性来源。build 是纯读。"""
    _append(journal, scope, EventType.USER_MESSAGE, {"content": "hi"}, "u1")

    ContextManager(journal, AgentConfig()).build(scope, ())

    assert journal.recover(scope).state.payload == {}


def test_no_system_prompt_means_no_system_message(journal: Journal, scope: Scope) -> None:
    _append(journal, scope, EventType.USER_MESSAGE, {"content": "hi"}, "u1")

    context = ContextManager(journal, AgentConfig()).build(scope, ())

    assert [m.role for m in context.messages] == ["user"]


def test_tools_are_passed_through(journal: Journal, scope: Scope) -> None:
    definition = ToolDefinition(name="echo", description="回显", input_schema={"type": "object"})

    context = ContextManager(journal, AgentConfig()).build(scope, (definition,))

    assert context.tools == (definition,)


def test_context_carries_the_state(journal: Journal, scope: Scope) -> None:
    context = ContextManager(journal, AgentConfig()).build(scope, ())

    assert context.state.scope == scope
