"""trace 的读模型：跨 session 不可见，找不到与无权查看不可区分。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from core.protocol.datatype.scope import Scope
from core.protocol.datatype.trace import Span, new_trace_id
from tests._support.biz_fixtures import LLM, SCOPE, STEP, TRACE, client


async def test_the_trace_list_counts_spans_steps_and_elapsed_time(tmp_path: Path) -> None:
    async with client(tmp_path, {SCOPE: (STEP, LLM)}) as (api, _runtime):
        listed = cast(list[dict[str, Any]], api.traces(SCOPE)["trace_ids"])

    assert listed == [{"trace_id": TRACE, "span_count": 2, "step_count": 1, "duration_ms": 2.0}]


async def test_a_trace_carries_every_span_with_its_own_duration(tmp_path: Path) -> None:
    async with client(tmp_path, {SCOPE: (STEP, LLM)}) as (api, _runtime):
        view = api.trace(SCOPE, TRACE)

    assert view["trace_id"] == TRACE
    assert view["duration_ms"] == 2.0
    spans = cast(list[dict[str, Any]], view["spans"])
    assert [span["span_id"] for span in spans] == [STEP.span_id, LLM.span_id]
    assert [span["duration_ms"] for span in spans] == [2.0, 0.5]
    assert spans[1]["parent_span_id"] == STEP.span_id


async def test_an_unknown_trace_id_looks_empty_rather_than_missing(tmp_path: Path) -> None:
    """与「无权查看」不可区分 —— 不留一个枚举 scope 里存过什么的旁路。"""
    async with client(tmp_path, {SCOPE: (STEP, LLM)}) as (api, _runtime):
        view = api.trace(SCOPE, new_trace_id())

    assert view["spans"] == []
    assert view["duration_ms"] is None


async def test_a_trace_of_another_session_is_not_visible(tmp_path: Path) -> None:
    other = Scope("u1", "s2")
    async with client(tmp_path, {other: (STEP, LLM)}) as (api, _runtime):
        assert api.traces(SCOPE)["trace_ids"] == []
        assert api.trace(SCOPE, TRACE)["spans"] == []
        assert api.traces(other)["trace_ids"] != []


async def test_a_trace_with_no_finished_span_has_no_elapsed_time(tmp_path: Path) -> None:
    """进程崩在 step 中途就是这个样子：认领写的那条还没有 end。"""
    open_step = Span(
        trace_id=new_trace_id(),
        span_id="0000000000000001",
        name="invoke_agent",
        start_unix_nano=1_000_000,
    )
    async with client(tmp_path, {SCOPE: (open_step,)}) as (api, _runtime):
        listed = cast(list[dict[str, Any]], api.traces(SCOPE)["trace_ids"])
        view = api.trace(SCOPE, open_step.trace_id)

    assert listed == [
        {
            "trace_id": open_step.trace_id,
            "span_count": 1,
            "step_count": 1,
            "duration_ms": None,
        }
    ]
    assert view["duration_ms"] is None
    assert cast(list[dict[str, Any]], view["spans"])[0]["end_unix_nano"] is None
