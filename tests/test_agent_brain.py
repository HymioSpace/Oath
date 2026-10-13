"""`AgentBrain` —— 一次 step 到底做了什么。"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest

from core.agent import AgentBrain
from core.protocol.datatype import AgentEvent, Cursor, CursorKind, EventType, Record, Scope
from core.protocol.datatype.config import AgentConfig, ModelProviderConfig
from core.protocol.datatype.llm import LlmRequest, LlmResponse, LlmToolCall
from core.protocol.datatype.tool import (
    BatchExecution,
    ToolCallRequest,
    ToolDefinition,
    ToolExecution,
)
from core.protocol.error import LlmError
from core.protocol.interface import AgentEventSink, DeltaSink, Journal, ModelProvider, ToolSystem
from tests._support.agent_fixtures import agent_ctx

# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------


class _Scripted(ModelProvider):
    """按脚本回话的 provider，顺带记下每次收到什么。

    `deltas` 给了就每次调用都先发这几段增量 —— 不给就一次不发，
    「有没有 delta 不改变其他行为」这条才验得到。
    """

    def __init__(
        self,
        *responses: LlmResponse,
        deltas: Sequence[tuple[str, str]] | None = None,
    ) -> None:
        self._responses = list(responses)
        self._deltas = list(deltas or [])
        self.requests: list[LlmRequest] = []

    def complete(self, request: LlmRequest, *, on_delta: DeltaSink | None = None) -> LlmResponse:
        self.requests.append(request)
        if not self._responses:
            raise LlmError("脚本里没有下一句了")
        if on_delta is not None:
            for channel, text in self._deltas:
                on_delta(channel, text)
        return self._responses.pop(0)


class _Broken(ModelProvider):
    """每次调用都失败。"""

    def complete(self, request: LlmRequest, *, on_delta: DeltaSink | None = None) -> LlmResponse:
        raise LlmError("模型挂了")


class _Sink(AgentEventSink):
    """记下投递出去的事件。`fail=True` 时每次都抛。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[AgentEvent] = []
        self._fail = fail

    def emit(self, event: AgentEvent) -> None:
        if self._fail:
            raise RuntimeError("投递失败")
        self.events.append(event)


class _Tools(ToolSystem):
    """记下派发请求，不真的跑。"""

    def __init__(self, *definitions: ToolDefinition) -> None:
        self._definitions = definitions
        self.batches: list[list[ToolCallRequest]] = []

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

    def execute(self, scope: Scope, requests: Sequence[ToolCallRequest]) -> BatchExecution:
        self.batches.append(list(requests))
        return BatchExecution()

    def status(self, scope: Scope, invocation_id: str) -> ToolExecution | None:
        return None

    def shutdown(self, *, wait: bool = True) -> None:
        return None


ECHO = ToolDefinition(name="echo", description="回显", input_schema={"type": "object"})


def _ids() -> Callable[[], str]:
    """定死 id 源，否则幂等键每次都不一样。"""
    counter = iter(f"id-{i}" for i in range(100))
    return lambda: next(counter)


def _brain(
    journal: Journal,
    provider: ModelProvider,
    tools: ToolSystem,
    sink: AgentEventSink,
    *,
    system_prompt: str = "你是助手",
) -> AgentBrain:
    config = AgentConfig(
        system_prompt=system_prompt,
        provider=ModelProviderConfig(model="m1", api_base="http://x", api_key_env="TEST_KEY"),
    )
    ctx = agent_ctx(journal, sink=sink, tools=tools, config=config)
    return AgentBrain(ctx, provider, id_gen=_ids())


def _types(journal: Journal, scope: Scope) -> list[str]:
    return [r.type for r in _records(journal, scope)]


def _records(journal: Journal, scope: Scope) -> list[Record]:
    return list(journal.read_range(scope, Cursor(CursorKind.LINE, 0)))


def _ask(journal: Journal, scope: Scope, *, text: str = "hi", key: str = "u1") -> Record:
    """落一条 `user.message`，把这条记录交回去 —— `step` 收的就是它。"""
    journal.append(
        scope,
        EventType.USER_MESSAGE,
        {"content": text},
        payload_schema_version=1,
        idempotency_key=key,
    )
    return _records(journal, scope)[-1]


def _answer(*tools: LlmToolCall) -> LlmResponse:
    return LlmResponse(content="我查一下", tool_calls=tools)


# --------------------------------------------------------------------------
# 只说话的一步
# --------------------------------------------------------------------------


def test_a_plain_answer_is_journalled(journal: Journal, scope: Scope) -> None:
    brain = _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), _Sink())

    brain.step(scope, _ask(journal, scope))

    assert _types(journal, scope) == [
        EventType.USER_MESSAGE,
        EventType.SYSTEM_AGENT_STEP,
        EventType.ASSISTANT_MESSAGE,
        EventType.SYSTEM_AGENT_STEP,
    ]


def test_the_request_carries_the_built_context(journal: Journal, scope: Scope) -> None:
    provider = _Scripted(LlmResponse(content="晴"))

    _brain(journal, provider, _Tools(ECHO), _Sink()).step(scope, _ask(journal, scope))

    request = provider.requests[0]
    assert [(m.role, m.content) for m in request.messages] == [
        ("system", "你是助手"),
        ("user", "hi"),
    ]
    assert request.tools == (ECHO,)
    assert request.model == "m1"


def test_the_model_name_is_recorded_with_the_answer(journal: Journal, scope: Scope) -> None:
    brain = _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), _Sink())

    brain.step(scope, _ask(journal, scope))

    assistant = next(r for r in _records(journal, scope) if r.type == EventType.ASSISTANT_MESSAGE)
    assert assistant.payload["model"] == "m1"


def test_no_tools_means_no_dispatch(journal: Journal, scope: Scope) -> None:
    tools = _Tools()

    _brain(journal, _Scripted(LlmResponse(content="晴")), tools, _Sink()).step(
        scope, _ask(journal, scope)
    )

    assert tools.batches == []


def test_the_answer_is_emitted(journal: Journal, scope: Scope) -> None:
    sink = _Sink()

    _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), sink).step(
        scope, _ask(journal, scope)
    )

    assert [e.type for e in sink.events] == ["assistant.message"]
    assert sink.events[0].payload["content"] == "晴"


def test_a_failing_sink_does_not_break_the_step(journal: Journal, scope: Scope) -> None:
    """事件已经落盘，客户端看不见只是界面少一行 —— 不能让整步失败。"""
    brain = _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), _Sink(fail=True))

    brain.step(scope, _ask(journal, scope))

    assert EventType.ASSISTANT_MESSAGE in _types(journal, scope)


# --------------------------------------------------------------------------
# 要调工具的一步
# --------------------------------------------------------------------------


def test_tool_calls_are_journalled_and_dispatched(journal: Journal, scope: Scope) -> None:
    tools = _Tools(ECHO)
    provider = _Scripted(_answer(LlmToolCall("echo", {"text": "hi"}, "t-1")))

    _brain(journal, provider, tools, _Sink()).step(scope, _ask(journal, scope))

    assert _types(journal, scope) == [
        EventType.USER_MESSAGE,
        EventType.SYSTEM_AGENT_STEP,
        EventType.ASSISTANT_MESSAGE,
        EventType.TOOL_CALL,
        EventType.SYSTEM_AGENT_STEP,
    ]
    assert [r.tool for r in tools.batches[0]] == ["echo"]


def test_the_models_call_id_is_passed_through(journal: Journal, scope: Scope) -> None:
    """模型给的 id 原样带回去 —— 它是 assistant 轮与 tool 消息之间的配对值。"""
    tools = _Tools(ECHO)
    provider = _Scripted(_answer(LlmToolCall("echo", {}, "call-from-model")))

    _brain(journal, provider, tools, _Sink()).step(scope, _ask(journal, scope))

    assert tools.batches[0][0].tool_call_id == "call-from-model"


def test_the_dispatch_id_is_oaths_own(journal: Journal, scope: Scope) -> None:
    """派发键不能借用模型给的 id —— 那只是对话历史里的配对值。"""
    tools = _Tools(ECHO)
    provider = _Scripted(_answer(LlmToolCall("echo", {}, "t-1")))

    _brain(journal, provider, tools, _Sink()).step(scope, _ask(journal, scope))

    assert tools.batches[0][0].invocation_id != "t-1"


def test_the_journalled_call_and_the_dispatch_share_one_invocation_id(
    journal: Journal, scope: Scope
) -> None:
    """同一个值落盘、也派发出去 —— 否则 `tool.result` 回来配不上那条 `tool.call`。"""
    tools = _Tools(ECHO)
    provider = _Scripted(_answer(LlmToolCall("echo", {}, "t-1")))

    _brain(journal, provider, tools, _Sink()).step(scope, _ask(journal, scope))

    journalled = next(r for r in _records(journal, scope) if r.type == EventType.TOOL_CALL)
    assert journalled.payload["invocation_id"] == tools.batches[0][0].invocation_id
    assert journalled.payload["tool_call_id"] == "t-1"


def test_the_arguments_reach_the_tool_untouched(journal: Journal, scope: Scope) -> None:
    tools = _Tools(ECHO)
    provider = _Scripted(_answer(LlmToolCall("echo", {"text": "hi", "n": 2}, "t-1")))

    _brain(journal, provider, tools, _Sink()).step(scope, _ask(journal, scope))

    assert tools.batches[0][0].arguments == {"text": "hi", "n": 2}


def test_several_tool_calls_each_get_a_record(journal: Journal, scope: Scope) -> None:
    tools = _Tools(ECHO)
    provider = _Scripted(
        _answer(
            LlmToolCall("echo", {"text": "a"}, "t-1"),
            LlmToolCall("echo", {"text": "b"}, "t-2"),
        )
    )

    _brain(journal, provider, tools, _Sink()).step(scope, _ask(journal, scope))

    assert _types(journal, scope).count(EventType.TOOL_CALL) == 2
    assert [r.tool_call_id for r in tools.batches[0]] == ["t-1", "t-2"]


def test_each_call_gets_its_own_invocation_id(journal: Journal, scope: Scope) -> None:
    """派发键重复就分不出谁是谁的结果了。"""
    tools = _Tools(ECHO)
    provider = _Scripted(_answer(LlmToolCall("echo", {}, "t-1"), LlmToolCall("echo", {}, "t-2")))

    _brain(journal, provider, tools, _Sink()).step(scope, _ask(journal, scope))

    assert tools.batches[0][0].invocation_id != tools.batches[0][1].invocation_id


def test_the_assistant_record_carries_the_call_refs(journal: Journal, scope: Scope) -> None:
    """投影靠 `assistant.message` 里的 tool_calls 给每个调用占位。"""
    provider = _Scripted(_answer(LlmToolCall("echo", {"text": "hi"}, "t-1")))

    _brain(journal, provider, _Tools(ECHO), _Sink()).step(scope, _ask(journal, scope))

    assistant = next(r for r in _records(journal, scope) if r.type == EventType.ASSISTANT_MESSAGE)
    assert assistant.payload["tool_calls"] == [
        {"id": "t-1", "name": "echo", "arguments": {"text": "hi"}}
    ]


def test_the_call_events_carry_the_pairing_ids(journal: Journal, scope: Scope) -> None:
    """事件也要带两个标识：客户端要拿它去显示工具卡片。"""
    sink = _Sink()
    provider = _Scripted(_answer(LlmToolCall("echo", {}, "t-1")))

    _brain(journal, provider, _Tools(ECHO), sink).step(scope, _ask(journal, scope))

    call = sink.events[1]
    assert call.type == "tool.call"
    assert call.tool_call_id == "t-1"
    assert call.invocation_id is not None
    assert call.tool == "echo"


def test_a_message_event_has_no_tool(journal: Journal, scope: Scope) -> None:
    """assistant 那一轮不是工具调用 —— 带个空 tool 字段就是撒谎。"""
    sink = _Sink()

    _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), sink).step(
        scope, _ask(journal, scope)
    )

    assert sink.events[0].tool is None
    assert sink.events[0].tool_call_id is None


# --------------------------------------------------------------------------
# 失败
# --------------------------------------------------------------------------


def test_a_failed_model_call_leaves_no_message(journal: Journal, scope: Scope) -> None:
    brain = _brain(journal, _Broken(), _Tools(), _Sink())

    with pytest.raises(LlmError):
        brain.step(scope, _ask(journal, scope))

    assert EventType.ASSISTANT_MESSAGE not in _types(journal, scope)


def test_a_failed_model_call_is_recorded_as_a_step_error(journal: Journal, scope: Scope) -> None:
    """客户端要知道这一步没跑完；只抛异常的话对端什么也看不到。"""
    brain = _brain(journal, _Broken(), _Tools(), _Sink())

    with pytest.raises(LlmError):
        brain.step(scope, _ask(journal, scope))

    steps = [
        r.payload["phase"]
        for r in _records(journal, scope)
        if r.type == EventType.SYSTEM_AGENT_STEP
    ]
    assert steps == ["start", "error"]


def test_a_failed_model_call_dispatches_nothing(journal: Journal, scope: Scope) -> None:
    tools = _Tools(ECHO)

    with pytest.raises(LlmError):
        _brain(journal, _Broken(), tools, _Sink()).step(scope, _ask(journal, scope))

    assert tools.batches == []


# --------------------------------------------------------------------------
# 流式
# --------------------------------------------------------------------------


def _streaming(deltas: Sequence[tuple[str, str]], *responses: LlmResponse) -> _Scripted:
    return _Scripted(*responses, deltas=deltas)


def test_deltas_reach_the_client(journal: Journal, scope: Scope) -> None:
    sink = _Sink()
    provider = _streaming([("content", "晴"), ("content", "天")], LlmResponse(content="晴天"))

    _brain(journal, provider, _Tools(), sink).step(scope, _ask(journal, scope))

    assert [e.payload["text"] for e in sink.events if e.type == "assistant.delta"] == ["晴", "天"]


def test_the_channel_is_carried_through(journal: Journal, scope: Scope) -> None:
    """客户端要靠它把「正在想」和「正在答」分开显示。"""
    sink = _Sink()
    provider = _streaming([("reasoning", "先想"), ("content", "后答")], LlmResponse(content="后答"))

    _brain(journal, provider, _Tools(), sink).step(scope, _ask(journal, scope))

    channels = [e.payload["channel"] for e in sink.events if e.type == "assistant.delta"]
    assert channels == ["reasoning", "content"]


def test_deltas_never_reach_the_journal(journal: Journal, scope: Scope) -> None:
    """一段增量在 journal 里是一句半话 —— append 它等于让记录说谎。"""
    provider = _streaming([("content", "晴")], LlmResponse(content="晴天"))

    _brain(journal, provider, _Tools(), _Sink()).step(scope, _ask(journal, scope))

    assert _types(journal, scope) == [
        EventType.USER_MESSAGE,
        EventType.SYSTEM_AGENT_STEP,
        EventType.ASSISTANT_MESSAGE,
        EventType.SYSTEM_AGENT_STEP,
    ]


def test_the_full_message_still_arrives_after_the_deltas(journal: Journal, scope: Scope) -> None:
    """断线重连靠它兜底 —— 它才是这一轮真正说过的话。"""
    sink = _Sink()
    provider = _streaming([("content", "晴")], LlmResponse(content="晴天"))

    _brain(journal, provider, _Tools(), sink).step(scope, _ask(journal, scope))

    assert [e.type for e in sink.events] == ["assistant.delta", "assistant.message"]
    assert sink.events[-1].payload["content"] == "晴天"


def test_deltas_carry_the_step_id(journal: Journal, scope: Scope) -> None:
    """并行几个 step 时靠它知道哪几段属于同一轮。"""
    sink = _Sink()
    provider = _streaming([("content", "晴")], LlmResponse(content="晴天"))

    _brain(journal, provider, _Tools(), sink).step(scope, _ask(journal, scope))

    delta = next(e for e in sink.events if e.type == "assistant.delta")
    step = next(r for r in _records(journal, scope) if r.type == EventType.SYSTEM_AGENT_STEP)
    assert delta.payload["step_id"] == step.payload["step_id"]


def test_delta_sequence_numbers_increase(journal: Journal, scope: Scope) -> None:
    sink = _Sink()
    provider = _streaming(
        [("content", "a"), ("content", "b"), ("content", "c")], LlmResponse(content="abc")
    )

    _brain(journal, provider, _Tools(), sink).step(scope, _ask(journal, scope))

    seqs = [e.payload["seq"] for e in sink.events if e.type == "assistant.delta"]
    assert seqs == [1, 2, 3]


def test_no_deltas_means_no_delta_events(journal: Journal, scope: Scope) -> None:
    """provider 一个增量都不给时，事件流与从前完全一样。"""
    sink = _Sink()

    _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), sink).step(
        scope, _ask(journal, scope)
    )

    assert [e.type for e in sink.events] == ["assistant.message"]


def test_a_failing_sink_does_not_break_a_streaming_step(journal: Journal, scope: Scope) -> None:
    """送不出去的一段增量不是模型调用失败 —— 这一步照样跑完、照样落盘。"""
    provider = _streaming([("content", "晴")], LlmResponse(content="晴天"))

    _brain(journal, provider, _Tools(), _Sink(fail=True)).step(scope, _ask(journal, scope))

    assert _types(journal, scope) == [
        EventType.USER_MESSAGE,
        EventType.SYSTEM_AGENT_STEP,
        EventType.ASSISTANT_MESSAGE,
        EventType.SYSTEM_AGENT_STEP,
    ]


def test_deltas_are_emitted_before_the_tool_call(journal: Journal, scope: Scope) -> None:
    """顺序反了，界面上会先看见「要调工具」，再看见它为什么。"""
    sink = _Sink()
    call = LlmToolCall(tool="echo", arguments={"text": "晴"}, tool_call_id="t-1")
    provider = _streaming([("content", "我查一下")], _answer(call))

    _brain(journal, provider, _Tools(ECHO), sink).step(scope, _ask(journal, scope))

    assert [e.type for e in sink.events] == [
        "assistant.delta",
        "assistant.message",
        "tool.call",
    ]
