"""一次 ReAct 循环在 trace 里长什么样。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from core.protocol.datatype.config import AgentConfig, ModelProviderConfig
from core.protocol.datatype.llm import LlmResponse, LlmToolCall
from core.protocol.datatype.trace import Span
from core.trace.attributes import (
    GEN_AI_REQUEST_MODEL,
    OATH_LLM_ATTEMPT,
    OATH_LLM_DURATION_MS,
    OATH_LLM_TPOT_MS,
    OATH_LLM_TTFT_MS,
    OATH_STEP_ID,
    OATH_STEP_LLM_CALLS,
    OATH_STEP_STATUS,
    REQUEST_EVENT,
    RESPONSE_EVENT,
)
from tests._support.intake_fixtures import Harness, until
from tests._support.tool_fixtures import EchoTool
from tests._support.trace_fixtures import BrokenTraceStore, RecordingTraceStore

#: 一次「模型要工具 → 工具回来 → 模型收尾」的完整循环。
LOOP = [LlmResponse(tool_calls=(LlmToolCall("a", {}, "id-a"),)), LlmResponse(content="好了")]

#: span 名带模型名，所以给 harness 配一个固定的模型。
CONFIG = AgentConfig(provider=ModelProviderConfig(model="m1"))


def _terminal(harness: Harness) -> bool:
    return all(step["status"] == "failed" for step in harness.state()["steps"].values())


def run_loop(harness: Harness) -> None:
    harness.start().send()
    harness.provider.wait(2)
    until(lambda: harness.state()["meta"].get("active_step") is None)


def store(harness: Harness) -> RecordingTraceStore:
    """Harness 上是 `TraceStore`，测试要看的 `written` 在替身上。"""
    return cast(RecordingTraceStore, harness.traces)


def spans_of(harness: Harness, name: str) -> list[Span]:
    """按名字取 span，走 `spans()` —— 同一个 span_id 认领写一次、终态写一次，
    这里看到的永远是终态那一条。"""
    traces = store(harness)
    [trace_id] = traces.trace_ids(harness.scope)
    return [span for span in traces.spans(harness.scope, trace_id) if span.name == name]


def step_spans(harness: Harness) -> list[Span]:
    return spans_of(harness, "invoke_agent")


def llm_spans(harness: Harness) -> list[Span]:
    return spans_of(harness, "chat m1")


def attr(span: Span, key: str) -> Any:
    """attribute 的值类型是 `JsonValue`，断言时按用到的形状断言就行。"""
    return span.attributes[key]


def body(span: Span, event_name: str) -> Any:
    """事件里的 `body` 是原始报文，序列化之后原样落盘。"""
    [event] = [event for event in span.events if event.name == event_name]
    return json.loads(str(event.attributes["body"]))


def test_one_loop_is_one_trace_with_a_span_per_step(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")], responses=LOOP, config=CONFIG) as harness:
        run_loop(harness)

        assert len(harness.traces.trace_ids(harness.scope)) == 1
        spans = harness.traces.spans(harness.scope, harness.traces.trace_ids(harness.scope)[0])
        steps = [s for s in spans if s.name == "invoke_agent"]
        assert len(steps) == 2
        # 两个 step 各自是顶层 span：第二步发生在第一步结束之后，套进去耗时就不对了。
        assert all(step.parent_span_id is None for step in steps)
        assert [s.start_unix_nano for s in steps] == sorted(s.start_unix_nano for s in steps)


def test_the_llm_span_hangs_off_its_step(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")], responses=LOOP, config=CONFIG) as harness:
        run_loop(harness)

        [trace_id] = harness.traces.trace_ids(harness.scope)
        steps = {
            s.span_id
            for s in harness.traces.spans(harness.scope, trace_id)
            if s.name == "invoke_agent"
        }
        for llm in harness.traces.spans(harness.scope, trace_id):
            if llm.name.startswith("chat"):
                assert llm.parent_span_id in steps


def test_a_step_without_tool_calls_closes_the_trace(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")], responses=LOOP, config=CONFIG) as harness:
        run_loop(harness)
        # 第二句是新的 run：必开新 trace，哪怕上一个 step 什么工具都没要。
        harness.send("再问一句", id="u2")
        harness.provider.wait(3)
        until(lambda: harness.state()["meta"].get("active_step") is None)

        assert len(harness.traces.trace_ids(harness.scope)) == 2


def test_the_request_and_response_land_in_the_span_verbatim(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")], responses=LOOP, config=CONFIG) as harness:
        run_loop(harness)

        first, _second = llm_spans(harness)
        names = [event.name for event in first.events]
        assert names == [REQUEST_EVENT, RESPONSE_EVENT]
        # 逐字对照 core 冻结的那一份，不复述字段。
        assert (
            body(first, REQUEST_EVENT)
            == harness.state()["steps"][attr(first, OATH_STEP_ID)]["request"]
        )
        assert body(first, RESPONSE_EVENT) == {
            "content": "",
            "reasoning": None,
            "finish_reason": "stop",
            "usage": {},
            "tool_calls": [{"id": "id-a", "name": "a", "arguments": {}}],
        }
        assert first.attributes[GEN_AI_REQUEST_MODEL] == "m1"
        assert attr(first, OATH_LLM_DURATION_MS) >= 0


def test_a_step_span_records_its_input_events_and_closes(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")], responses=LOOP, config=CONFIG) as harness:
        run_loop(harness)

        first, second = step_spans(harness)
        # 认领写一次、终态再写一次；读的时候只认后写的，所以看到的是 completed。
        assert first.attributes[OATH_STEP_STATUS] == "completed"
        assert first.attributes[OATH_STEP_LLM_CALLS] == 1
        assert first.end_unix_nano is not None
        assert [event.name for event in first.events] == ["oath.input.user"]
        assert body(first, "oath.input.user")["payload"] == {"content": "你好"}
        # 第二步的输入是工具结果，不是用户消息 —— 这一步被工具结果唤醒。
        assert [event.name for event in second.events] == ["oath.input.tool"]


def test_ttft_and_tpot_need_a_streamed_answer(tmp_path: Path) -> None:
    def stream(request: object) -> LlmResponse:
        return LlmResponse(content="一二三四五六七八九十")

    with Harness(tmp_path, tools=[EchoTool("a")], responses=[stream], config=CONFIG) as harness:
        harness.start().send()
        harness.provider.wait(1)
        until(lambda: harness.state()["meta"].get("active_step") is None)

        [llm] = llm_spans(harness)
        assert OATH_LLM_TTFT_MS not in llm.attributes
        assert OATH_LLM_TPOT_MS not in llm.attributes


def test_a_failed_model_call_is_recorded_as_an_error_span(tmp_path: Path) -> None:
    def explode(request: object) -> LlmResponse:
        raise RuntimeError("模型炸了")

    # 铺满尝试次数：脚本用尽之后 provider 会回一句默认响应，那样就不是全失败了。
    # 重试间隔调到零，否则要按默认间隔等三次。
    config = replace(CONFIG, llm_retry_delay=0.001)
    with Harness(
        tmp_path,
        tools=[EchoTool("a")],
        responses=[explode] * config.llm_max_attempts,
        config=config,
    ) as harness:
        harness.start().send()
        until(lambda: harness.state()["steps"] and _terminal(harness))

        # 每次尝试一条 span：重试了几次就能看出几次，这次失败之前问过几次。
        spans = llm_spans(harness)
        assert [attr(span, OATH_LLM_ATTEMPT) for span in spans] == list(range(1, len(spans) + 1))
        for span in spans:
            assert span.status == "error"
            # 请求进去了，响应没有。
            assert [event.name for event in span.events] == [REQUEST_EVENT]


def test_a_broken_trace_store_does_not_stop_inference(tmp_path: Path) -> None:
    traces = BrokenTraceStore()
    with Harness(tmp_path, tools=[EchoTool("a")], responses=LOOP, traces=traces) as harness:
        run_loop(harness)

        assert harness.provider.wait(2).messages[-1].role == "tool"
        assert harness.state()["conversation"]["messages"][-1]["content"] == "好了"
        assert not traces.written


def test_the_same_trace_store_is_reused_across_scopes(tmp_path: Path) -> None:
    traces = RecordingTraceStore()
    with Harness(tmp_path, tools=[EchoTool("a")], responses=LOOP, traces=traces) as harness:
        run_loop(harness)

    assert [scope for scope, _ in traces.written] == [harness.scope] * len(traces.written)
