"""会话的写入面：身份绑定、输入接纳与只读命令的分派。"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.protocol.datatype.wire import Command
from core.protocol.error import WireProtocolError
from tests._support.biz_fixtures import LLM, SCOPE, STEP, TRACE, answer, client


async def test_a_command_for_another_session_is_rejected(tmp_path: Path) -> None:
    async with client(tmp_path, {}) as (api, _runtime):
        with pytest.raises(WireProtocolError):
            api.submit(Command("u1", "user.message", "s2", {"content": "你好"}), SCOPE)


async def test_a_non_text_message_is_rejected_before_it_is_written(tmp_path: Path) -> None:
    async with client(tmp_path, {}) as (api, runtime):
        with pytest.raises(WireProtocolError):
            api.submit(Command("u1", "user.message", SCOPE.session_id, {"content": 42}), SCOPE)

        assert not runtime.ctx.store.read(SCOPE)["inputs"]


async def test_a_decision_without_an_invocation_is_rejected(tmp_path: Path) -> None:
    async with client(tmp_path, {}) as (api, _runtime):
        with pytest.raises(WireProtocolError):
            api.submit(
                Command(
                    "d1",
                    "permission.decide",
                    SCOPE.session_id,
                    {"invocation_id": "nope", "status": "maybe"},
                ),
                SCOPE,
            )


async def test_answer_routes_queries_and_ignores_everything_else(tmp_path: Path) -> None:
    """查询回 (event_type, payload)，非查询返回 None —— 传输层据此决定 ACK 还是事件。"""
    async with client(tmp_path, {SCOPE: (STEP, LLM)}) as (api, _runtime):
        event_type, payload = await answer(
            api, Command("q1", "trace.get", SCOPE.session_id, {"trace_id": TRACE}), SCOPE
        )
        assert event_type == "trace.get"
        assert payload["duration_ms"] == 2.0

        event_type, _ = await answer(api, Command("q2", "trace.list", SCOPE.session_id, {}), SCOPE)
        assert event_type == "trace.list"

        assert (
            api.answer(Command("u1", "user.message", SCOPE.session_id, {"content": "hi"}), SCOPE)
            is None
        )


async def test_a_query_does_not_admit_an_input(tmp_path: Path) -> None:
    """查询命令不进 buffer，也不认领 step —— 重发一百次和发一次一样。"""
    async with client(tmp_path, {}) as (api, runtime):
        for index in range(3):
            await answer(api, Command(f"q{index}", "trace.list", SCOPE.session_id, {}), SCOPE)

        state = runtime.ctx.store.read(SCOPE)
        assert not state["inputs"]
        assert state["meta"].get("active_step") is None
