"""跨模块协议：终态可独立持久化，消息视图按 step 固定。"""

from pathlib import Path
from typing import Any, cast

from core.context.intake import validate_messages
from core.journal.messages import project
from core.journal.validation import validate_payload
from core.protocol.datatype import Cursor, CursorKind, EventType
from core.protocol.datatype.llm import LlmResponse, LlmToolCall
from tests._support.intake_fixtures import Harness, requests, until
from tests._support.tool_fixtures import EchoTool, VerdictPolicy


def test_a_terminal_is_a_fact_on_its_own(tmp_path: Path) -> None:
    """终态自己就是事实 —— 输入还没被 Brain 认领，它照样完整落着。"""
    with Harness(tmp_path, tools=[EchoTool("a")]) as h:
        h.tools.execute(h.scope, requests("a"))
        until(lambda: h.call("a")["terminal"])
        records = list(h.journal.read_range(h.scope, Cursor(CursorKind.LINE, 0)))
        terminal = next(r for r in records if r.payload.get("reason") == "tool.terminal")

        # `changes` 的形状正是这个测试要断言的东西，wire 上只有 JsonValue。
        changes = cast("dict[str, dict[str, Any]]", terminal.payload["changes"])
        assert changes["calls"]["a"]["terminal"]
        assert "inputs" not in changes


def test_terminal_payload_is_valid_public_contract(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")]) as h:
        h.tools.execute(h.scope, requests("a"))
        until(lambda: h.call("a")["terminal"])
        validate_payload(EventType.TOOL_RESULT, 1, h.call("a")["terminal"])


def test_journal_projection_uses_frozen_request_history(tmp_path: Path) -> None:
    response = LlmResponse(tool_calls=(LlmToolCall("a", {}, "tool-a"),))
    with Harness(tmp_path, tools=[EchoTool("a")], responses=[response]) as h:
        h.start().send()
        h.provider.wait(2)
        until(lambda: h.state()["meta"].get("active_step") is None)
        records = list(h.journal.read_range(h.scope, Cursor(CursorKind.LINE, 0)))
        messages = project(records)
        assert messages[-1].role == "assistant"
        validate_messages([m.to_dict() for m in messages])
        assert len([m for m in messages if m.role == "tool"]) == 1


def test_waiting_approval_is_state_not_a_terminal_record(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")], policy=VerdictPolicy(default="ask")) as h:
        h.tools.execute(h.scope, requests("a"))
        until(lambda: h.call("a")["status"] == "waiting_approval")
        assert not h.call("a")["terminal"]
        assert h.state()["inputs"] == {}
        execution = h.tools.status(h.scope, "a")
        assert execution is not None
        assert not execution.is_terminal
