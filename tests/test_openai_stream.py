"""流式 —— SSE chunk 怎么攒成一个 `LlmResponse`。

**不发请求。** 直接喂 `_StreamAccumulator`，所以测的是「拿到这些 chunk 会不会攒对」，
不是网络。HTTP 那层（`httpx.stream` 怎么开、错误响应怎么读）由手工 e2e 兜。
"""

from __future__ import annotations

import pytest

from core.llm.openai_provider import _StreamAccumulator
from core.protocol.datatype.llm import LlmFinishReason, LlmResponse
from core.protocol.error import LlmError
from core.protocol.interface.llm import DeltaChannel
from core.utils.encoding import JsonObject, JsonValue


def _collect(*chunks: JsonValue) -> tuple[LlmResponse, list[tuple[str, str]]]:
    """喂一串 chunk，返回结果和收到过的增量。"""
    seen: list[tuple[str, str]] = []
    accumulator = _StreamAccumulator(lambda channel, text: seen.append((channel, text)))
    for chunk in chunks:
        accumulator.feed(chunk)
    return accumulator.result(), seen


def _text(text: str, **extra: JsonValue) -> JsonObject:
    """一个只带文本的 delta chunk。"""
    return {"choices": [{"index": 0, "delta": {"content": text, **extra}}]}


def _calls(index: int, **function: JsonValue) -> JsonObject:
    """一个只带 tool call 片段的 delta chunk。"""
    return {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": index, **function}]}}]}


# --------------------------------------------------------------------------
# 文本
# --------------------------------------------------------------------------


def test_content_chunks_are_joined_in_order() -> None:
    result, _ = _collect(_text("链路"), _text("通"), _text("了"))

    assert result.content == "链路通了"


def test_each_chunk_is_emitted_as_it_arrives() -> None:
    """边收边发 —— 客户端能在整段还没到时就渲染。"""
    _, seen = _collect(_text("a"), _text("b"), _text("c"))

    assert seen == [("content", "a"), ("content", "b"), ("content", "c")]


def test_reasoning_goes_on_its_own_channel() -> None:
    """两路合在一起，客户端就分不清「正在想」和「正在答」。"""
    _, seen = _collect(
        {"choices": [{"delta": {"reasoning_content": "先想"}}]},
        _text("后答"),
    )

    assert seen == [("reasoning", "先想"), (DeltaChannel.CONTENT, "后答")]


def test_reasoning_and_content_are_collected_separately() -> None:
    result, _ = _collect(
        {"choices": [{"delta": {"reasoning_content": "想"}}]},
        _text("说"),
    )

    assert result.reasoning == "想"
    assert result.content == "说"


def test_no_reasoning_at_all_is_none() -> None:
    """与非流式路径保持一致：journal 里两种「没有」长得一样。"""
    result, _ = _collect(_text("只有答案"))

    assert result.reasoning is None


def test_the_alternative_reasoning_key_also_works() -> None:
    result, seen = _collect({"choices": [{"delta": {"reasoning": "换了名字"}}]})

    assert result.reasoning == "换了名字"
    assert seen == [("reasoning", "换了名字")]


def test_empty_chunks_produce_no_delta() -> None:
    """ "`audio_content: ""` 这类空字段各家都在发，不能变成一条空增量。"""
    _, seen = _collect(
        {"choices": [{"delta": {"role": "assistant", "content": "", "audio_content": ""}}]}
    )

    assert seen == []


# --------------------------------------------------------------------------
# tool call
# --------------------------------------------------------------------------


def test_a_tool_call_in_one_chunk_is_complete() -> None:
    """MiniMax 就是这么发的：arguments 整段一次到。"""
    result, seen = _collect(
        _calls(
            0,
            id="c-1",
            function={"name": "echo", "arguments": '{"text": "链路通了"}'},
        )
    )

    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert (call.tool_call_id, call.tool, call.arguments) == ("c-1", "echo", {"text": "链路通了"})
    # 参数不进 delta —— 半截 JSON 对客户端没有用处。
    assert seen == []


def test_a_tool_call_split_across_chunks_is_rejoined() -> None:
    """OpenAI 就是这么发的：arguments 一段一段到。"""
    result, _ = _collect(
        _calls(0, id="c-1", function={"name": "echo", "arguments": '{"te'}),
        _calls(0, function={"arguments": 'xt": "链路通了"}'}),
    )

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].arguments == {"text": "链路通了"}


def test_two_tool_calls_are_kept_apart_by_index() -> None:
    """两段交错着来 —— 靠 index 认谁是谁，不靠顺序。"""
    result, _ = _collect(
        _calls(0, id="a", function={"name": "echo", "arguments": '{"text": '}),
        _calls(1, id="b", function={"name": "clock", "arguments": '{"tz": '}),
        _calls(0, function={"arguments": '"甲"}'}),
        _calls(1, function={"arguments": '"乙"}'}),
    )

    assert [(c.tool_call_id, c.tool, c.arguments) for c in result.tool_calls] == [
        ("a", "echo", {"text": "甲"}),
        ("b", "clock", {"tz": "乙"}),
    ]


def test_a_repeated_name_does_not_overwrite_the_real_one() -> None:
    """有的厂家每段都重复带一遍 name。"""
    result, _ = _collect(
        _calls(0, id="c-1", function={"name": "echo", "arguments": "{}"}),
        _calls(0, function={"name": "echo", "arguments": ""}),
    )

    assert result.tool_calls[0].tool == "echo"


def test_broken_arguments_still_raise() -> None:
    """攒完还是坏的 JSON 时抛错，不当成空参数放过去。"""
    with pytest.raises(LlmError):
        _collect(_calls(0, id="c-1", function={"name": "echo", "arguments": '{"te'}))


# --------------------------------------------------------------------------
# 收尾
# --------------------------------------------------------------------------


def test_finish_reason_is_taken_from_the_last_chunk() -> None:
    result, _ = _collect(
        _text("话"),
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    )

    assert result.finish_reason == LlmFinishReason.TOOL_CALLS


def test_usage_only_chunks_are_taken() -> None:
    """各家都在最后补一个只有 usage 的 chunk。"""
    result, _ = _collect(
        _text("话"),
        {"choices": [], "usage": {"total_tokens": 12}},
    )

    assert result.usage == {"total_tokens": 12}


def test_a_chunk_without_choices_is_not_an_error() -> None:
    """没 choices 的 chunk 不该把一次成功的调用变成失败。"""
    result, _ = _collect(_text("话"), {"id": "x", "object": "chat.completion.chunk"})

    assert result.content == "话"


def test_a_stream_that_never_carries_a_choice_is_a_failure() -> None:
    """HTTP 200 + 一个报错帧 + `[DONE]`：请求成功收到了，模型一个字也没说。

    当成正常的空回答收下，agent 就把这段空话落进对话历史，然后清掉 buffer ——
    用户那句话就这么消失了，日志里还留着一条说得过去很正常的记录。
    """
    with pytest.raises(LlmError):
        _collect({"error": {"message": "provider unavailable", "type": "server_error"}})


def test_an_empty_stream_is_a_failure() -> None:
    with pytest.raises(LlmError):
        _collect()


def test_an_error_frame_before_the_answer_is_a_failure() -> None:
    """先说了半句再报错：半句也不能当答案收下。"""
    with pytest.raises(LlmError):
        _collect(_text("半句"), {"error": {"message": "provider unavailable"}})


def test_a_delta_that_cannot_be_delivered_does_not_break_the_call() -> None:
    """送不出去的一段增量不是模型调用失败 —— 返回值仍然是完整的那一份。"""

    def boom(channel: str, text: str) -> None:
        raise RuntimeError("投递失败")

    accumulator = _StreamAccumulator(boom)
    accumulator.feed(_text("话"))

    assert accumulator.result().content == "话"


# --------------------------------------------------------------------------
# 形状不对
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chunk",
    ["not an object", 42, ["a"], None],
    ids=["string", "int", "list", "null"],
)
def test_a_chunk_that_is_not_an_object_raises(chunk: JsonValue) -> None:
    with pytest.raises(LlmError):
        _StreamAccumulator(None).feed(chunk)


def test_a_choice_that_is_not_an_object_raises() -> None:
    with pytest.raises(LlmError):
        _StreamAccumulator(None).feed({"choices": ["nope"]})


def test_a_tool_call_entry_that_is_not_an_object_raises() -> None:
    with pytest.raises(LlmError):
        _StreamAccumulator(None).feed({"choices": [{"delta": {"tool_calls": ["nope"]}}]})
