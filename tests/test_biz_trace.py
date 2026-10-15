"""biz 层：按 trace_id 取回一条 trace。

用真实装配与文件后端 —— 这里要验的正是「落到磁盘上的东西能不能原样取回来」，
替身绕过了 span 序列化那一段。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from biz.trace import get_trace, list_traces
from bootstrap.runtime import Runtime
from core.protocol.datatype.config import OathConfig
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.trace import Span, SpanStatus, new_trace_id
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


@pytest.fixture
def runtime(tmp_path: Path) -> Runtime:
    return Runtime(
        OathConfig(
            data_root=tmp_path,
            credentials={"synthetic-test-token": (SCOPE.user_id, SCOPE.session_id)},
        ),
        loop=asyncio.new_event_loop(),
        provider=ScriptedProvider(),
    )


def write(runtime: Runtime, scope: Scope, *spans: Span) -> None:
    for span in spans:
        runtime.traces.append(scope, span)


def test_the_trace_list_counts_spans_steps_and_elapsed_time(runtime: Runtime) -> None:
    write(runtime, SCOPE, STEP, LLM)

    listed = cast(list[dict[str, Any]], list_traces(runtime.traces, SCOPE)["trace_ids"])

    assert listed == [{"trace_id": TRACE, "span_count": 2, "step_count": 1, "duration_ms": 2.0}]
    runtime.close()


def test_a_trace_carries_every_span_with_its_own_duration(runtime: Runtime) -> None:
    write(runtime, SCOPE, STEP, LLM)

    view = get_trace(runtime.traces, SCOPE, TRACE)

    assert view["trace_id"] == TRACE
    assert view["duration_ms"] == 2.0
    spans = cast(list[dict[str, Any]], view["spans"])
    assert [span["span_id"] for span in spans] == [STEP.span_id, LLM.span_id]
    assert [span["duration_ms"] for span in spans] == [2.0, 0.5]
    assert spans[1]["parent_span_id"] == STEP.span_id
    runtime.close()


def test_an_unknown_trace_id_looks_empty_rather_than_missing(runtime: Runtime) -> None:
    """与「无权查看」不可区分 —— 不留一个枚举 scope 里存过什么的旁路。"""
    write(runtime, SCOPE, STEP, LLM)

    view = get_trace(runtime.traces, SCOPE, new_trace_id())

    assert view["spans"] == []
    assert view["duration_ms"] is None
    runtime.close()


def test_a_trace_of_another_session_is_not_visible(runtime: Runtime) -> None:
    other = Scope("u1", "s2")
    write(runtime, other, STEP, LLM)

    assert list_traces(runtime.traces, SCOPE)["trace_ids"] == []
    assert get_trace(runtime.traces, SCOPE, TRACE)["spans"] == []
    assert list_traces(runtime.traces, other)["trace_ids"] != []
    runtime.close()


def test_a_trace_with_no_finished_span_has_no_elapsed_time(runtime: Runtime) -> None:
    """进程崩在 step 中途就是这个样子：认领时写的那条还没有 end。"""
    trace_id = new_trace_id()
    write(runtime, SCOPE, Span(trace_id, "0000000000000001", "invoke_agent", 1_000_000))

    listed = cast(list[dict[str, Any]], list_traces(runtime.traces, SCOPE)["trace_ids"])
    view = get_trace(runtime.traces, SCOPE, trace_id)

    assert listed == [{"trace_id": trace_id, "span_count": 1, "step_count": 1, "duration_ms": None}]
    assert view["duration_ms"] is None
    assert cast(list[dict[str, Any]], view["spans"])[0]["end_unix_nano"] is None
    runtime.close()
