"""biz 层：按 trace_id 取回一条 trace。

用真实装配与文件后端 —— 这里要验的正是「落到磁盘上的东西能不能原样取回来」，
替身绕过了 span 序列化那一段。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from biz.trace import list_traces, query_trace
from bootstrap.runtime import Runtime
from core.protocol.datatype.config import OathConfig
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.trace import Span, SpanStatus, new_trace_id
from core.protocol.datatype.wire import Command, CommandType
from core.utils.encoding import JsonObject
from tests._support.intake_fixtures import ScriptedProvider, until

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


def page(runtime: Runtime, scope: Scope = SCOPE) -> JsonObject:
    """列一页。参数固定 —— 分页自己另有测试。"""
    return list_traces(runtime.traces, scope, limit=50, offset=0)


def test_the_trace_list_counts_spans_steps_and_elapsed_time(runtime: Runtime) -> None:
    write(runtime, SCOPE, STEP, LLM)

    listed = cast(list[dict[str, Any]], page(runtime)["trace_ids"])

    assert listed == [{"trace_id": TRACE, "span_count": 2, "step_count": 1, "duration_ms": 2.0}]
    runtime.close()


def test_a_trace_carries_every_span_with_its_own_duration(runtime: Runtime) -> None:
    write(runtime, SCOPE, STEP, LLM)

    view = query_trace(runtime.traces, SCOPE, TRACE)

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

    view = query_trace(runtime.traces, SCOPE, new_trace_id())

    assert view["spans"] == []
    assert view["duration_ms"] is None
    runtime.close()


def test_a_trace_of_another_session_is_not_visible(runtime: Runtime) -> None:
    other = Scope("u1", "s2")
    write(runtime, other, STEP, LLM)

    assert page(runtime)["trace_ids"] == []
    assert query_trace(runtime.traces, SCOPE, TRACE)["spans"] == []
    assert page(runtime, other)["trace_ids"] != []
    runtime.close()


def test_a_trace_with_no_finished_span_has_no_elapsed_time(runtime: Runtime) -> None:
    """进程崩在 step 中途就是这个样子：认领时写的那条还没有 end。"""
    trace_id = new_trace_id()
    write(runtime, SCOPE, Span(trace_id, "0000000000000001", "invoke_agent", 1_000_000))

    listed = cast(list[dict[str, Any]], page(runtime)["trace_ids"])
    view = query_trace(runtime.traces, SCOPE, trace_id)

    assert listed == [{"trace_id": trace_id, "span_count": 1, "step_count": 1, "duration_ms": None}]
    assert view["duration_ms"] is None
    assert cast(list[dict[str, Any]], view["spans"])[0]["end_unix_nano"] is None
    runtime.close()


def test_the_status_keeps_the_trace_id_after_the_step_is_over(runtime: Runtime) -> None:
    """刚跑完的那一轮要还能查到 —— 只从 active step 取的话，step 一结束就断了。"""
    runtime.submit(
        Command("u1", CommandType.USER_MESSAGE, SCOPE.session_id, {"content": "你好"}), SCOPE
    )
    until(lambda: runtime.status(SCOPE)["trace_id"] is not None)
    until(lambda: runtime.status(SCOPE)["step_id"] is None)

    trace_id = str(runtime.status(SCOPE)["trace_id"])
    listed = cast(list[dict[str, Any]], page(runtime)["trace_ids"])
    assert [item["trace_id"] for item in listed] == [trace_id]
    assert query_trace(runtime.traces, SCOPE, trace_id)["duration_ms"] is not None
    runtime.close()


def test_the_trace_list_pages_by_offset_over_the_whole_set(runtime: Runtime) -> None:
    """`total` 是全部条数，不是这一页的 —— 客户端靠它判断还有没有下一页。"""
    ids = [new_trace_id() for _ in range(3)]
    for trace_id in ids:
        write(runtime, SCOPE, Span(trace_id, "0000000000000001", "invoke_agent", 1_000_000))

    first = list_traces(runtime.traces, SCOPE, limit=2, offset=0)
    second = list_traces(runtime.traces, SCOPE, limit=2, offset=2)
    past = list_traces(runtime.traces, SCOPE, limit=2, offset=9)

    # 最近写入的排前面，所以后写的 ids[2] 在第一页。
    assert [item["trace_id"] for item in cast(list[dict[str, Any]], first["trace_ids"])] == [
        ids[2],
        ids[1],
    ]
    assert [item["trace_id"] for item in cast(list[dict[str, Any]], second["trace_ids"])] == [
        ids[0]
    ]
    assert (first["total"], first["limit"], first["offset"]) == (3, 2, 0)
    # 翻过头是空页，不是报错。
    assert past["trace_ids"] == []
    assert past["total"] == 3
    runtime.close()
