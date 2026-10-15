"""biz 层：操作与查询。trace 查询走真实的 Runtime 与文件后端。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest

from biz.api import AgentApi
from bootstrap.runtime import Runtime
from core.protocol.datatype.config import OathConfig
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.trace import Span, SpanStatus, new_trace_id
from core.protocol.datatype.wire import Command
from core.protocol.error import WireProtocolError
from tests._support.intake_fixtures import ScriptedProvider

TRACE = new_trace_id()
SCOPE = Scope("u1", "s1")

STEP = Span(
    trace_id=TRACE,
    span_id="0000000000000001",
    name="invoke_agent",
    start_unix_nano=1_000_000,
    end_unix_nano=3_000_000,
    status=SpanStatus.OK,
    attributes={"oath.step.id": "s"},
)
LLM = Span(
    trace_id=TRACE,
    span_id="0000000000000002",
    parent_span_id=STEP.span_id,
    name="chat m1",
    start_unix_nano=2_000_000,
    end_unix_nano=2_500_000,
    status=SpanStatus.OK,
)


@asynccontextmanager
async def client(tmp_path: Path, spans: dict[Scope, tuple[Span, ...]]) -> AsyncIterator[AgentApi]:
    """真装配一份 Runtime，span 直接写进它的文件后端。"""
    runtime = Runtime(
        OathConfig(
            data_root=tmp_path,
            credentials={"synthetic-test-token": (SCOPE.user_id, SCOPE.session_id)},
        ),
        loop=asyncio.get_running_loop(),
        provider=ScriptedProvider(),
    )
    for scope, written in spans.items():
        for span in written:
            runtime.ctx.traces.append(scope, span)
    try:
        yield AgentApi(runtime)
    finally:
        await asyncio.to_thread(runtime.close)


async def test_the_trace_list_counts_spans_steps_and_elapsed_time(tmp_path: Path) -> None:
    async with client(tmp_path, {SCOPE: (STEP, LLM)}) as api:
        listed = cast(
            list[dict[str, Any]], (await asyncio.to_thread(api.traces, SCOPE))["trace_ids"]
        )

    assert listed == [{"trace_id": TRACE, "span_count": 2, "step_count": 1, "duration_ms": 2.0}]


async def test_a_trace_carries_every_span_with_its_own_duration(tmp_path: Path) -> None:
    async with client(tmp_path, {SCOPE: (STEP, LLM)}) as api:
        view = await asyncio.to_thread(api.trace, SCOPE, TRACE)

    assert view["trace_id"] == TRACE
    assert view["duration_ms"] == 2.0
    spans = cast(list[dict[str, Any]], view["spans"])
    assert [span["span_id"] for span in spans] == [STEP.span_id, LLM.span_id]
    assert [span["duration_ms"] for span in spans] == [2.0, 0.5]
    assert spans[1]["parent_span_id"] == STEP.span_id


async def test_an_unknown_trace_id_looks_empty_rather_than_missing(tmp_path: Path) -> None:
    """与「无权查看」不可区分 —— 不留一个枚举 scope 里存过什么的旁路。"""
    async with client(tmp_path, {SCOPE: (STEP, LLM)}) as api:
        view = await asyncio.to_thread(api.trace, SCOPE, new_trace_id())

    assert view["spans"] == []
    assert view["duration_ms"] is None


async def test_a_trace_of_another_session_is_not_visible(tmp_path: Path) -> None:
    other = Scope("u1", "s2")
    async with client(tmp_path, {other: (STEP, LLM)}) as api:
        assert (await asyncio.to_thread(api.traces, SCOPE))["trace_ids"] == []
        assert (await asyncio.to_thread(api.trace, SCOPE, TRACE))["spans"] == []
        assert (await asyncio.to_thread(api.traces, other))["trace_ids"] != []


async def test_a_command_for_another_session_is_rejected(tmp_path: Path) -> None:
    async with client(tmp_path, {}) as api:
        with pytest.raises(WireProtocolError):
            await asyncio.to_thread(
                api.submit, Command("u1", "user.message", "s2", {"content": "你好"}), SCOPE
            )
