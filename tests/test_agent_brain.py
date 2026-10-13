"""`AgentBrain` —— 一次 step 到底做了什么，以及什么时候不该做。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import pytest

from core.agent import AgentBrain
from core.protocol.datatype import (
    AgentEvent,
    Batch,
    Cursor,
    CursorKind,
    EventType,
    Inbound,
    InboundKind,
    Record,
    Scope,
)
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
) -> AgentBrain:
    config = AgentConfig(
        system_prompt="你是助手",
        provider=ModelProviderConfig(model="m1", api_base="http://x", api_key_env="TEST_KEY"),
    )
    ctx = agent_ctx(journal, sink=sink, tools=tools, config=config)
    return AgentBrain(ctx, provider, id_gen=_ids())


def _types(journal: Journal, scope: Scope) -> list[str]:
    return [r.type for r in _records(journal, scope)]


def _records(journal: Journal, scope: Scope) -> list[Record]:
    return list(journal.read_range(scope, Cursor(CursorKind.LINE, 0)))


def _of(journal: Journal, scope: Scope, event_type: str) -> list[Record]:
    return [r for r in _records(journal, scope) if r.type == event_type]


# -- 造输入 ---------------------------------------------------------------


def _user(content: str = "hi", *, command_id: str = "c-1") -> Inbound:
    return Inbound(
        kind=InboundKind.USER,
        source="user.message",
        payload={"content": content},
        source_id=command_id,
        occur_dt="2026-10-13T00:00:00.000000Z",
    )


def _result(invocation_id: str, *, content: str = "ok", seconds_ago: float = 0.0) -> Inbound:
    # payload 逐字段对齐 `tool.result` 的 schema —— 工具名不在里面，那是事件的顶层
    # 字段，要用回 journal 里的 `tool.call` 查。
    return Inbound(
        kind=InboundKind.TOOL_RESULT,
        source="tool.terminal",
        payload={
            "tool_call_id": f"tc-{invocation_id}",
            "invocation_id": invocation_id,
            "outcome": "succeeded",
            "content": content,
        },
        source_id=invocation_id,
        occur_dt=(datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat(),
    )


def _cancel(command_id: str = "c-9") -> Inbound:
    return Inbound(
        kind=InboundKind.INVOKE,
        source="run.cancel",
        payload={"run_id": command_id},
        source_id=command_id,
    )


def _answer(*tools: LlmToolCall) -> LlmResponse:
    return LlmResponse(content="我查一下", tool_calls=tools)


# --------------------------------------------------------------------------
# 只说话的一步
# --------------------------------------------------------------------------


def test_a_plain_answer_is_journalled(journal: Journal, scope: Scope) -> None:
    brain = _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), _Sink())

    brain.step(scope, Batch(user=(_user(),)))

    assert _types(journal, scope) == [
        EventType.USER_MESSAGE,
        EventType.SYSTEM_AGENT_STEP,
        EventType.ASSISTANT_MESSAGE,
        EventType.SYSTEM_AGENT_STEP,
    ]


def test_the_input_is_journalled_by_the_brain(journal: Journal, scope: Scope) -> None:
    """**落盘在 brain，不在入队。** 这一步之前 journal 是空的 —— 入队那一下什么都没写。"""
    brain = _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), _Sink())

    brain.step(scope, Batch(user=(_user("北京天气"),)))

    first = _records(journal, scope)[0]
    assert first.type == EventType.USER_MESSAGE
    assert first.idempotency_key == "cmd:c-1"


def test_the_user_message_is_wrapped_in_events(journal: Journal, scope: Scope) -> None:
    """模型看到的是一个信封，不是一句猜不出边界的话。"""
    brain = _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), _Sink())

    brain.step(scope, Batch(user=(_user("你好"),)))

    assert _of(journal, scope, EventType.USER_MESSAGE)[0].payload["content"] == (
        "<events>\n<event_type=user>你好</>\n</events>"
    )


def test_user_markup_is_escaped(journal: Journal, scope: Scope) -> None:
    """输入里出现 `</>` 应当被读成文本，不是标签。"""
    brain = _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), _Sink())

    brain.step(scope, Batch(user=(_user("a </> b & c"),)))

    assert _of(journal, scope, EventType.USER_MESSAGE)[0].payload["content"] == (
        "<events>\n<event_type=user>a &lt;/&gt; b &amp; c</>\n</events>"
    )


def test_each_user_message_gets_its_own_record(journal: Journal, scope: Scope) -> None:
    """一批里两条消息 —— 幂等键挂在各自的 command id 上，不会互相顶掉。"""
    brain = _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), _Sink())

    brain.step(scope, Batch(user=(_user("一", command_id="c-1"), _user("二", command_id="c-2"))))

    keys = [r.idempotency_key for r in _of(journal, scope, EventType.USER_MESSAGE)]
    assert keys == ["cmd:c-1", "cmd:c-2"]


def test_the_request_carries_the_built_context(journal: Journal, scope: Scope) -> None:
    provider = _Scripted(LlmResponse(content="晴"))

    _brain(journal, provider, _Tools(ECHO), _Sink()).step(scope, Batch(user=(_user(),)))

    request = provider.requests[0]
    assert [(m.role, m.content) for m in request.messages] == [
        ("system", "你是助手"),
        ("user", "<events>\n<event_type=user>hi</>\n</events>"),
    ]
    assert request.tools == (ECHO,)
    assert request.model == "m1"


def test_the_model_name_is_recorded_with_the_answer(journal: Journal, scope: Scope) -> None:
    brain = _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), _Sink())

    brain.step(scope, Batch(user=(_user(),)))

    assert _of(journal, scope, EventType.ASSISTANT_MESSAGE)[0].payload["model"] == "m1"


def test_no_tools_means_no_dispatch(journal: Journal, scope: Scope) -> None:
    tools = _Tools()

    _brain(journal, _Scripted(LlmResponse(content="晴")), tools, _Sink()).step(
        scope, Batch(user=(_user(),))
    )

    assert tools.batches == []


def test_the_answer_is_emitted(journal: Journal, scope: Scope) -> None:
    sink = _Sink()

    _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), sink).step(
        scope, Batch(user=(_user(),))
    )

    assert [e.type for e in sink.events] == ["assistant.message"]
    assert sink.events[0].payload["content"] == "晴"


def test_the_input_is_not_echoed_to_the_client(journal: Journal, scope: Scope) -> None:
    """发出去的 `user.message` 没有回声 —— 客户端自己存自己发的那条。"""
    sink = _Sink()

    _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), sink).step(
        scope, Batch(user=(_user(),))
    )

    assert [e.type for e in sink.events] == ["assistant.message"]


def test_the_trigger_is_recorded(journal: Journal, scope: Scope) -> None:
    """没有它，trace 里分不清「用户问了」和「工具回来了」。"""
    brain = _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), _Sink())

    brain.step(scope, Batch(user=(_user(),)))

    triggers = [r.payload["trigger"] for r in _of(journal, scope, EventType.SYSTEM_AGENT_STEP)]
    assert triggers == ["user", "user"]


def test_a_failing_sink_does_not_break_the_step(journal: Journal, scope: Scope) -> None:
    """事件已经落盘，客户端看不见只是界面少一行 —— 不能让整步失败。"""
    brain = _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), _Sink(fail=True))

    brain.step(scope, Batch(user=(_user(),)))

    assert EventType.ASSISTANT_MESSAGE in _types(journal, scope)


# --------------------------------------------------------------------------
# 要调工具的一步
# --------------------------------------------------------------------------


def test_tool_calls_are_journalled_and_dispatched(journal: Journal, scope: Scope) -> None:
    tools = _Tools(ECHO)
    provider = _Scripted(_answer(LlmToolCall("echo", {"text": "hi"}, "t-1")))

    _brain(journal, provider, tools, _Sink()).step(scope, Batch(user=(_user(),)))

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

    _brain(journal, provider, tools, _Sink()).step(scope, Batch(user=(_user(),)))

    assert tools.batches[0][0].tool_call_id == "call-from-model"


def test_the_dispatch_id_is_oaths_own(journal: Journal, scope: Scope) -> None:
    """派发键不能借用模型给的 id —— 那只是对话历史里的配对值。"""
    tools = _Tools(ECHO)
    provider = _Scripted(_answer(LlmToolCall("echo", {}, "t-1")))

    _brain(journal, provider, tools, _Sink()).step(scope, Batch(user=(_user(),)))

    assert tools.batches[0][0].invocation_id != "t-1"


def test_the_journalled_call_and_the_dispatch_share_one_invocation_id(
    journal: Journal, scope: Scope
) -> None:
    """同一个值落盘、也派发出去 —— 否则 `tool.result` 回来配不上那条 `tool.call`。"""
    tools = _Tools(ECHO)
    provider = _Scripted(_answer(LlmToolCall("echo", {}, "t-1")))

    _brain(journal, provider, tools, _Sink()).step(scope, Batch(user=(_user(),)))

    journalled = _of(journal, scope, EventType.TOOL_CALL)[0]
    assert journalled.payload["invocation_id"] == tools.batches[0][0].invocation_id
    assert journalled.payload["tool_call_id"] == "t-1"


def test_the_arguments_reach_the_tool_untouched(journal: Journal, scope: Scope) -> None:
    tools = _Tools(ECHO)
    provider = _Scripted(_answer(LlmToolCall("echo", {"text": "hi", "n": 2}, "t-1")))

    _brain(journal, provider, tools, _Sink()).step(scope, Batch(user=(_user(),)))

    assert tools.batches[0][0].arguments == {"text": "hi", "n": 2}


def test_several_tool_calls_each_get_a_record(journal: Journal, scope: Scope) -> None:
    tools = _Tools(ECHO)
    provider = _Scripted(
        _answer(
            LlmToolCall("echo", {"text": "a"}, "t-1"),
            LlmToolCall("echo", {"text": "b"}, "t-2"),
        )
    )

    _brain(journal, provider, tools, _Sink()).step(scope, Batch(user=(_user(),)))

    assert _types(journal, scope).count(EventType.TOOL_CALL) == 2
    assert [r.tool_call_id for r in tools.batches[0]] == ["t-1", "t-2"]


def test_each_call_gets_its_own_invocation_id(journal: Journal, scope: Scope) -> None:
    """派发键重复就分不出谁是谁的结果了。"""
    tools = _Tools(ECHO)
    provider = _Scripted(_answer(LlmToolCall("echo", {}, "t-1"), LlmToolCall("echo", {}, "t-2")))

    _brain(journal, provider, tools, _Sink()).step(scope, Batch(user=(_user(),)))

    assert tools.batches[0][0].invocation_id != tools.batches[0][1].invocation_id


def test_the_assistant_record_carries_the_call_refs(journal: Journal, scope: Scope) -> None:
    """投影靠 `assistant.message` 里的 tool_calls 给每个调用占位。"""
    provider = _Scripted(_answer(LlmToolCall("echo", {"text": "hi"}, "t-1")))

    _brain(journal, provider, _Tools(ECHO), _Sink()).step(scope, Batch(user=(_user(),)))

    assistant = _of(journal, scope, EventType.ASSISTANT_MESSAGE)[0]
    assert assistant.payload["tool_calls"] == [
        {"id": "t-1", "name": "echo", "arguments": {"text": "hi"}}
    ]


def test_the_call_events_carry_the_pairing_ids(journal: Journal, scope: Scope) -> None:
    """事件也要带两个标识：客户端要拿它去显示工具卡片。"""
    sink = _Sink()
    provider = _Scripted(_answer(LlmToolCall("echo", {}, "t-1")))

    _brain(journal, provider, _Tools(ECHO), sink).step(scope, Batch(user=(_user(),)))

    call = sink.events[1]
    assert call.type == "tool.call"
    assert call.tool_call_id == "t-1"
    assert call.invocation_id is not None
    assert call.tool == "echo"


def test_a_message_event_has_no_tool(journal: Journal, scope: Scope) -> None:
    """assistant 那一轮不是工具调用 —— 带个空 tool 字段就是撒谎。"""
    sink = _Sink()

    _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), sink).step(
        scope, Batch(user=(_user(),))
    )

    assert sink.events[0].tool is None
    assert sink.events[0].tool_call_id is None


# --------------------------------------------------------------------------
# 工具结果回来
# --------------------------------------------------------------------------


def _round_of(
    journal: Journal, scope: Scope, count: int, *after: LlmResponse
) -> tuple[AgentBrain, list[str], _Scripted]:
    """走完一轮派发 `count` 个调用，返回 (brain, invocation_id 们, provider)。

    `after` 是后面几步的台词 —— 这一步自己吃掉第一句（要调工具的那句），
    后面的按顺序留给后续调用。
    """
    calls = tuple(LlmToolCall("echo", {}, f"t-{i}") for i in range(count))
    provider = _Scripted(_answer(*calls), *after)
    tools = _Tools(ECHO)
    brain = _brain(journal, provider, tools, _Sink())
    brain.step(scope, Batch(user=(_user(),)))
    return brain, [str(r.invocation_id) for r in tools.batches[0]], provider


def _round_of_two(
    journal: Journal, scope: Scope, *after: LlmResponse
) -> tuple[AgentBrain, list[str], _Scripted]:
    return _round_of(journal, scope, 2, *after)


def test_a_settled_round_wakes_the_brain(journal: Journal, scope: Scope) -> None:
    brain, ids, provider = _round_of_two(journal, scope, LlmResponse(content="都查到了"))

    assert brain.step(scope, Batch(tool=(_result(ids[0]), _result(ids[1])))) is True
    assert len(provider.requests) == 2  # 派发那一轮 + 结果到了这一轮


def test_an_unsettled_round_does_not_wake_the_brain(journal: Journal, scope: Scope) -> None:
    """五个回来两个 —— 拿半个答案去猜，剩下三个槽位在模型那边是空的。"""
    brain, ids, provider = _round_of_two(journal, scope, LlmResponse(content="都查到了"))
    before = len(provider.requests)

    assert brain.step(scope, Batch(tool=(_result(ids[0]),))) is False
    assert len(provider.requests) == before


def test_an_unsettled_round_journals_nothing(journal: Journal, scope: Scope) -> None:
    """返回 False 之前什么都不写 —— buffer 不清是因为这一步压根没算数。"""
    brain, ids, _ = _round_of_two(journal, scope, LlmResponse(content="都查到了"))
    before = _types(journal, scope)

    brain.step(scope, Batch(tool=(_result(ids[0]),)))

    assert _types(journal, scope) == before


def test_results_are_journalled_by_the_brain(journal: Journal, scope: Scope) -> None:
    """producer 不碰 journal。落盘是读到这一批之后的判断。"""
    brain, ids, _ = _round_of_two(journal, scope, LlmResponse(content="好了"))

    brain.step(scope, Batch(tool=(_result(ids[0]), _result(ids[1]))))

    journalled = _of(journal, scope, EventType.TOOL_RESULT)
    assert [r.payload["invocation_id"] for r in journalled] == ids


def test_a_result_fills_the_projection_slot(journal: Journal, scope: Scope) -> None:
    """槽位还开着 —— 投影把它填回原来的位置，不另起一条消息。"""
    brain, ids, provider = _round_of_two(journal, scope, LlmResponse(content="好了"))

    brain.step(scope, Batch(tool=(_result(ids[0]), _result(ids[1]))))

    assert [(m.role, m.content) for m in provider.requests[-1].messages[1:]] == [
        ("user", "<events>\n<event_type=user>hi</>\n</events>"),
        ("assistant", "我查一下"),
        ("tool", "ok"),
        ("tool", "ok"),
    ]


def test_a_replayed_result_does_not_add_a_second_line(journal: Journal, scope: Scope) -> None:
    """幂等键挂在 invocation_id 上 —— 崩溃后同一批重跑一次不会多出第二行。"""
    brain, ids, _ = _round_of_two(
        journal, scope, LlmResponse(content="一"), LlmResponse(content="二")
    )
    brain.step(scope, Batch(tool=(_result(ids[0]),)))

    brain.step(scope, Batch(tool=(_result(ids[0]), _result(ids[1]))))

    journalled = _of(journal, scope, EventType.TOOL_RESULT)
    assert [r.payload["invocation_id"] for r in journalled] == ids


def test_nothing_pending_means_a_tool_batch_is_actionable(journal: Journal, scope: Scope) -> None:
    """没有挂着的调用时，工具结果没有「等谁」的问题 —— 直接处理。"""
    brain = _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), _Sink())

    assert brain.step(scope, Batch(tool=(_result("orphan"),))) is True


def test_an_empty_batch_is_not_actionable(journal: Journal, scope: Scope) -> None:
    brain = _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), _Sink())

    assert brain.step(scope, Batch()) is False


# --------------------------------------------------------------------------
# 等不到齐的时候
# --------------------------------------------------------------------------


def test_wakeup_delay_is_none_once_the_round_is_settled(journal: Journal, scope: Scope) -> None:
    brain, ids, _ = _round_of_two(journal, scope)

    assert brain.wakeup_delay(scope, Batch(tool=(_result(ids[0]), _result(ids[1])))) is None


def test_wakeup_delay_counts_from_the_first_result(journal: Journal, scope: Scope) -> None:
    """慢的是工具，不是 agent 决定等它 —— 从第一个结果到达起算，不是从派发起算。"""
    brain, ids, _ = _round_of_two(journal, scope)

    delay = brain.wakeup_delay(scope, Batch(tool=(_result(ids[0]),)))

    assert delay == pytest.approx(30.0, abs=1.0)


def test_wakeup_delay_uses_the_earliest_result(journal: Journal, scope: Scope) -> None:
    """先到的那个定时限 —— 后到的那个不该把已经等过的时间抹掉。"""
    brain, ids, _ = _round_of(journal, scope, 3)

    delay = brain.wakeup_delay(
        scope, Batch(tool=(_result(ids[0], seconds_ago=5.0), _result(ids[2])))
    )

    assert delay == pytest.approx(25.0, abs=1.0)


def test_wakeup_delay_is_none_for_user_input(journal: Journal, scope: Scope) -> None:
    """有用户输入就是纯等新输入，不是等时间。"""
    brain = _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), _Sink())

    assert brain.wakeup_delay(scope, Batch(user=(_user(),))) is None


def test_wakeup_delay_is_none_with_nothing_pending(journal: Journal, scope: Scope) -> None:
    brain = _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), _Sink())

    assert brain.wakeup_delay(scope, Batch(tool=(_result("orphan"),))) is None


def test_an_expired_round_wakes_the_brain(journal: Journal, scope: Scope) -> None:
    """没回来的那几个不会往队列里 put 任何东西 —— 到点就得自己醒。
    没有这一条，这一轮永远停在 buffer 里，agent 再也不说话。"""
    brain, ids, provider = _round_of_two(journal, scope, LlmResponse(content="第一个的"))
    before = len(provider.requests)

    assert brain.step(scope, Batch(tool=(_result(ids[0], seconds_ago=31.0),))) is True
    assert len(provider.requests) == before + 1


def test_an_unexpired_round_keeps_waiting(journal: Journal, scope: Scope) -> None:
    brain, ids, _ = _round_of_two(journal, scope, LlmResponse(content="第一个的"))

    assert brain.step(scope, Batch(tool=(_result(ids[0], seconds_ago=29.0),))) is False


def test_the_expired_round_leaves_a_placeholder(journal: Journal, scope: Scope) -> None:
    """缺的那个槽位必须还在 —— 模型那边的 tool 消息不允许缺一个配对。"""
    brain, ids, provider = _round_of_two(journal, scope, LlmResponse(content="第一个的"))

    brain.step(scope, Batch(tool=(_result(ids[0], seconds_ago=31.0),)))

    messages = provider.requests[-1].messages
    tool_messages = [m for m in messages if m.role == "tool"]
    assert len(tool_messages) == 2
    unresolved = [m for m in tool_messages if m.extra.get("unresolved")]
    assert len(unresolved) == 1
    assert unresolved[0].extra["tool_call_id"] == "t-1"  # 第二个调用没回来


# --------------------------------------------------------------------------
# 迟到的结果
# --------------------------------------------------------------------------


def _timed_out(journal: Journal, scope: Scope) -> tuple[AgentBrain, list[str]]:
    """走到「两个调用里只回来一个，模型已经答过一次」的状态。

    这是超时那条路的真实形状：先到齐之前的批、等到期、这一步跑了，第二个还没回来。
    """
    brain, ids, _ = _round_of_two(
        journal, scope, LlmResponse(content="只有第一个"), LlmResponse(content="第二个也来了")
    )
    brain.step(scope, Batch(tool=(_result(ids[0], seconds_ago=31.0),)))
    return brain, ids


def test_a_late_result_goes_into_the_user_message(journal: Journal, scope: Scope) -> None:
    """槽位已经被一个 `assistant.message` 关上了 —— 那个占位是模型读过的记录，
    回头改它就是让记录说谎。"""
    brain, ids = _timed_out(journal, scope)

    brain.step(scope, Batch(tool=(_result(ids[1]),)))

    users = _of(journal, scope, EventType.USER_MESSAGE)
    assert users[-1].payload["content"] == (
        "<events>\n"
        f'<event_type=tool_result invocation_id="{ids[1]}" tool="echo">'
        f"之前 id 为 {ids[1]} 的工具结果为：ok</>\n"
        "</events>"
    )


def test_a_late_result_is_not_written_as_a_tool_result(journal: Journal, scope: Scope) -> None:
    """写成 `tool.result` 的话，投影会把它填回那个已经关掉的槽位。"""
    brain, ids = _timed_out(journal, scope)

    brain.step(scope, Batch(tool=(_result(ids[1]),)))

    journalled = _of(journal, scope, EventType.TOOL_RESULT)
    assert [r.payload["invocation_id"] for r in journalled] == [ids[0]]


def test_a_late_result_is_told_alongside_new_user_input(journal: Journal, scope: Scope) -> None:
    brain, ids = _timed_out(journal, scope)

    brain.step(
        scope,
        Batch(user=(_user("顺便问一下", command_id="c-5"),), tool=(_result(ids[1]),)),
    )

    content = str(_of(journal, scope, EventType.USER_MESSAGE)[-1].payload["content"])
    assert "<event_type=user>顺便问一下</>" in content
    assert f'invocation_id="{ids[1]}"' in content


def test_a_late_result_lands_in_the_first_of_two_user_messages(
    journal: Journal, scope: Scope
) -> None:
    """两条用户输入 —— 补告并进第一条，不凭空多出一条消息。"""
    brain, ids = _timed_out(journal, scope)

    brain.step(
        scope,
        Batch(
            user=(_user("一", command_id="c-5"), _user("二", command_id="c-6")),
            tool=(_result(ids[1]),),
        ),
    )

    contents = [str(r.payload["content"]) for r in _of(journal, scope, EventType.USER_MESSAGE)]
    assert contents[-2].startswith("<events>\n<event_type=user>一</>\n<event_type=tool_result")
    assert contents[-1] == "<events>\n<event_type=user>二</>\n</events>"


def test_a_late_result_alone_is_its_own_message(journal: Journal, scope: Scope) -> None:
    """没有用户输入时，补告自己就是那一条消息 —— 幂等键由参与它的调用定住。"""
    brain, ids = _timed_out(journal, scope)
    before = len(_records(journal, scope))

    brain.step(scope, Batch(tool=(_result(ids[1]),)))

    added = [r for r in _records(journal, scope)[before:] if r.type == EventType.USER_MESSAGE]
    assert len(added) == 1
    assert added[0].idempotency_key == f"notice:{ids[1]}"


def test_the_same_late_result_is_not_written_twice(journal: Journal, scope: Scope) -> None:
    """重放一次（崩溃后重来）不该多出一条补告 —— 幂等键挡住了。"""
    brain, ids, _ = _round_of_two(
        journal,
        scope,
        LlmResponse(content="只有第一个"),
        LlmResponse(content="第二个也来了"),
        LlmResponse(content="又说一遍"),
    )
    brain.step(scope, Batch(tool=(_result(ids[0], seconds_ago=31.0),)))
    brain.step(scope, Batch(tool=(_result(ids[1]),)))

    brain.step(scope, Batch(tool=(_result(ids[1]),)))

    assert len(_of(journal, scope, EventType.USER_MESSAGE)) == 2  # 用户那句 + 补告


def test_the_late_result_still_reaches_the_model(journal: Journal, scope: Scope) -> None:
    """补告的用处就是被模型读到 —— 记下来不算数。"""
    brain, ids, provider = _round_of_two(
        journal, scope, LlmResponse(content="只有第一个"), LlmResponse(content="第二个也来了")
    )
    brain.step(scope, Batch(tool=(_result(ids[0], seconds_ago=31.0),)))

    brain.step(scope, Batch(tool=(_result(ids[1]),)))

    assert provider.requests[-1].messages[-1].content == (
        "<events>\n"
        f'<event_type=tool_result invocation_id="{ids[1]}" tool="echo">'
        f"之前 id 为 {ids[1]} 的工具结果为：ok</>\n"
        "</events>"
    )


# --------------------------------------------------------------------------
# 控制面
# --------------------------------------------------------------------------


def test_a_cancel_is_journalled_without_calling_the_model(journal: Journal, scope: Scope) -> None:
    """还没有可中止的 run —— 记一笔就结束，调模型是白花钱。"""
    provider = _Scripted(LlmResponse(content="不该被调用"))
    brain = _brain(journal, provider, _Tools(), _Sink())

    assert brain.step(scope, Batch(system=(_cancel(),))) is True
    assert provider.requests == []
    assert _of(journal, scope, EventType.SYSTEM_CANCEL)


def test_the_cancel_trigger_is_recorded(journal: Journal, scope: Scope) -> None:
    brain = _brain(journal, _Scripted(), _Tools(), _Sink())

    brain.step(scope, Batch(system=(_cancel(),)))

    triggers = [r.payload["trigger"] for r in _of(journal, scope, EventType.SYSTEM_AGENT_STEP)]
    assert triggers == ["cancel", "cancel"]


def test_a_cancel_journals_the_tool_results_alongside_it(journal: Journal, scope: Scope) -> None:
    """输入先落盘再判断该不该调模型 —— 同批里那个结果不丢，下一轮模型照样看得到。"""
    brain, ids, provider = _round_of_two(journal, scope, LlmResponse(content="一"))
    before = len(_types(journal, scope))

    brain.step(scope, Batch(system=(_cancel(),), tool=(_result(ids[0]),)))

    assert _types(journal, scope)[before:] == [
        EventType.TOOL_RESULT,
        EventType.SYSTEM_CANCEL,
        EventType.SYSTEM_AGENT_STEP,
        EventType.SYSTEM_AGENT_STEP,
    ]
    assert len(provider.requests) == 1  # 只有派发那一轮


# --------------------------------------------------------------------------
# 失败
# --------------------------------------------------------------------------


def test_a_failed_model_call_leaves_no_message(journal: Journal, scope: Scope) -> None:
    brain = _brain(journal, _Broken(), _Tools(), _Sink())

    with pytest.raises(LlmError):
        brain.step(scope, Batch(user=(_user(),)))

    assert EventType.ASSISTANT_MESSAGE not in _types(journal, scope)


def test_a_failed_model_call_is_recorded_as_a_step_error(journal: Journal, scope: Scope) -> None:
    """客户端要知道这一步没跑完；只抛异常的话对端什么也看不到。"""
    brain = _brain(journal, _Broken(), _Tools(), _Sink())

    with pytest.raises(LlmError):
        brain.step(scope, Batch(user=(_user(),)))

    phases = [r.payload["phase"] for r in _of(journal, scope, EventType.SYSTEM_AGENT_STEP)]
    assert phases == ["start", "error"]


def test_a_failed_model_call_dispatches_nothing(journal: Journal, scope: Scope) -> None:
    tools = _Tools(ECHO)

    with pytest.raises(LlmError):
        _brain(journal, _Broken(), tools, _Sink()).step(scope, Batch(user=(_user(),)))

    assert tools.batches == []


def test_the_input_survives_a_failed_model_call(journal: Journal, scope: Scope) -> None:
    """输入已经落盘了 —— 下一轮补上模型就行，不用等用户重发。"""
    brain = _brain(journal, _Broken(), _Tools(), _Sink())

    with pytest.raises(LlmError):
        brain.step(scope, Batch(user=(_user(),)))

    assert _of(journal, scope, EventType.USER_MESSAGE)


# --------------------------------------------------------------------------
# 流式
# --------------------------------------------------------------------------


def _streaming(deltas: Sequence[tuple[str, str]], *responses: LlmResponse) -> _Scripted:
    return _Scripted(*responses, deltas=deltas)


def test_deltas_reach_the_client(journal: Journal, scope: Scope) -> None:
    sink = _Sink()
    provider = _streaming([("content", "晴"), ("content", "天")], LlmResponse(content="晴天"))

    _brain(journal, provider, _Tools(), sink).step(scope, Batch(user=(_user(),)))

    texts = [e.payload["text"] for e in sink.events if e.type == "assistant.delta"]
    assert texts == ["晴", "天"]


def test_the_channel_is_carried_through(journal: Journal, scope: Scope) -> None:
    """客户端要靠它把「正在想」和「正在答」分开显示。"""
    sink = _Sink()
    provider = _streaming([("reasoning", "先想"), ("content", "后答")], LlmResponse(content="后答"))

    _brain(journal, provider, _Tools(), sink).step(scope, Batch(user=(_user(),)))

    channels = [e.payload["channel"] for e in sink.events if e.type == "assistant.delta"]
    assert channels == ["reasoning", "content"]


def test_deltas_never_reach_the_journal(journal: Journal, scope: Scope) -> None:
    """一段增量在 journal 里是一句半话 —— append 它等于让记录说谎。"""
    provider = _streaming([("content", "晴")], LlmResponse(content="晴天"))

    _brain(journal, provider, _Tools(), _Sink()).step(scope, Batch(user=(_user(),)))

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

    _brain(journal, provider, _Tools(), sink).step(scope, Batch(user=(_user(),)))

    assert [e.type for e in sink.events] == ["assistant.delta", "assistant.message"]
    assert sink.events[-1].payload["content"] == "晴天"


def test_deltas_carry_the_step_id(journal: Journal, scope: Scope) -> None:
    """并行几个 step 时靠它知道哪几段属于同一轮。"""
    sink = _Sink()
    provider = _streaming([("content", "晴")], LlmResponse(content="晴天"))

    _brain(journal, provider, _Tools(), sink).step(scope, Batch(user=(_user(),)))

    delta = next(e for e in sink.events if e.type == "assistant.delta")
    step = _of(journal, scope, EventType.SYSTEM_AGENT_STEP)[0]
    assert delta.payload["step_id"] == step.payload["step_id"]


def test_delta_sequence_numbers_increase(journal: Journal, scope: Scope) -> None:
    sink = _Sink()
    provider = _streaming(
        [("content", "a"), ("content", "b"), ("content", "c")], LlmResponse(content="abc")
    )

    _brain(journal, provider, _Tools(), sink).step(scope, Batch(user=(_user(),)))

    seqs = [e.payload["seq"] for e in sink.events if e.type == "assistant.delta"]
    assert seqs == [1, 2, 3]


def test_no_deltas_means_no_delta_events(journal: Journal, scope: Scope) -> None:
    """provider 一个增量都不给时，事件流与从前完全一样。"""
    sink = _Sink()

    _brain(journal, _Scripted(LlmResponse(content="晴")), _Tools(), sink).step(
        scope, Batch(user=(_user(),))
    )

    assert [e.type for e in sink.events] == ["assistant.message"]


def test_a_failing_sink_does_not_break_a_streaming_step(journal: Journal, scope: Scope) -> None:
    """送不出去的一段增量不是模型调用失败 —— 这一步照样跑完、照样落盘。"""
    provider = _streaming([("content", "晴")], LlmResponse(content="晴天"))

    _brain(journal, provider, _Tools(), _Sink(fail=True)).step(scope, Batch(user=(_user(),)))

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

    _brain(journal, provider, _Tools(ECHO), sink).step(scope, Batch(user=(_user(),)))

    assert [e.type for e in sink.events] == [
        "assistant.delta",
        "assistant.message",
        "tool.call",
    ]
