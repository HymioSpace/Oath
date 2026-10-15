"""biz 层测试的公共装配：真 Runtime + 真文件 trace 后端。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from biz import AgentApi
from bootstrap.runtime import Runtime
from core.protocol.datatype.config import OathConfig
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.trace import Span, SpanStatus, new_trace_id
from core.protocol.datatype.wire import Command
from core.utils.encoding import JsonObject
from tests._support.intake_fixtures import ScriptedProvider

SCOPE = Scope("u1", "s1")

#: 一条两步的 trace：step span 与它下面的 llm span。
TRACE = new_trace_id()
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
async def client(
    tmp_path: Path, spans: dict[Scope, tuple[Span, ...]]
) -> AsyncIterator[tuple[AgentApi, Runtime]]:
    """起一份真 Runtime，span 直接写进它的文件后端。"""
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
        yield AgentApi(runtime), runtime
    finally:
        await asyncio.to_thread(runtime.close)


async def answer(api: AgentApi, command: Command, scope: Scope) -> tuple[str, JsonObject]:
    """`answer` 对查询命令回二元组，对其余返回 None —— 这里要的是前者。"""
    result = await asyncio.to_thread(api.answer, command, scope)
    assert result is not None, f"{command.type} 不是查询命令"
    return result


__all__ = ["LLM", "SCOPE", "STEP", "TRACE", "answer", "client"]
