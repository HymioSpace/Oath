"""`OpenAICompatibleProvider` 的翻译 —— 请求体与响应体。

全部是纯函数测试：**不发请求**。发请求那段只有 api key 的存在性检查值得测，
其余的（重试、超时分类）没有实现，写了就是给不存在的行为写测试。
"""

from __future__ import annotations

import pytest

from core.llm.openai_provider import (
    OpenAICompatibleProvider,
    build_payload,
    parse_response,
    to_wire_message,
    to_wire_tools,
)
from core.protocol.datatype.config import ModelProviderConfig
from core.protocol.datatype.llm import LlmFinishReason, LlmRequest, LlmResponse
from core.protocol.datatype.message import Message
from core.protocol.datatype.tool import ToolDefinition
from core.protocol.error import LlmError
from core.utils.encoding import JsonObject, JsonValue

ECHO = ToolDefinition(
    name="echo", description="回显", input_schema={"type": "object", "properties": {"text": {}}}
)


def _request(*messages: Message, **kwargs: object) -> LlmRequest:
    return LlmRequest(messages=list(messages), **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Message → 请求体
# --------------------------------------------------------------------------


def test_a_message_keeps_its_role_and_content() -> None:
    wire = to_wire_message(Message(role="user", content="你好"))

    assert wire == {"role": "user", "content": "你好"}


def test_extra_fields_are_passed_through() -> None:
    """`tool_call_id` 是模型 API 认的配对值 —— 不能在适配层被吃掉。"""
    wire = to_wire_message(Message(role="tool", content="ok", extra={"tool_call_id": "t-1"}))

    assert wire["tool_call_id"] == "t-1"


def test_oaths_own_markers_are_not_sent() -> None:
    """`outcome` / `unresolved` 是给投影和客户端看的，模型 API 不认。"""
    wire = to_wire_message(
        Message(role="tool", content="ok", extra={"outcome": "succeeded", "unresolved": False})
    )

    assert "outcome" not in wire
    assert "unresolved" not in wire


def test_flat_tool_calls_become_the_nested_openai_shape() -> None:
    """core 里存的是扁平的 `{id, name, arguments}`，wire 上要多一层。"""
    wire = to_wire_message(
        Message(
            role="assistant",
            content="",
            extra={"tool_calls": [{"id": "t-1", "name": "echo", "arguments": {"text": "hi"}}]},
        )
    )

    assert wire["tool_calls"] == [
        {
            "id": "t-1",
            "type": "function",
            "function": {"name": "echo", "arguments": '{"text": "hi"}'},
        }
    ]


def test_an_assistant_message_without_calls_has_no_tool_calls_field() -> None:
    wire = to_wire_message(Message(role="assistant", content="晴"))

    assert "tool_calls" not in wire


# --------------------------------------------------------------------------
# tools → 请求体
# --------------------------------------------------------------------------


def test_a_definition_becomes_a_function_tool() -> None:
    tools = to_wire_tools(_request(tools=(ECHO,)))

    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "回显",
                "parameters": ECHO.input_schema,
            },
        }
    ]


def test_no_tools_means_no_field() -> None:
    """给个空数组的话有些厂家会报错 —— 键干脆不出现。"""
    assert to_wire_tools(_request()) is None


# --------------------------------------------------------------------------
# 整个请求体
# --------------------------------------------------------------------------


def test_the_payload_carries_the_model_and_the_messages() -> None:
    payload = build_payload(_request(Message(role="user", content="你好"), model="m1"), "m1")

    assert payload["model"] == "m1"
    assert payload["messages"] == [{"role": "user", "content": "你好"}]


def test_streaming_is_off() -> None:
    """这一版只有非流式 —— 流式的处理路径还不存在。"""
    payload = build_payload(_request(), "m1")

    assert payload["stream"] is False


def test_temperature_is_left_out_when_unset() -> None:
    """发 `temperature: null` 会被有些厂家拒收。"""
    payload = build_payload(_request(temperature=None), "m1")

    assert "temperature" not in payload


def test_temperature_is_sent_when_set() -> None:
    assert build_payload(_request(temperature=0.2), "m1")["temperature"] == 0.2


def test_extra_params_win() -> None:
    """调用点显式给的参数是最具体的一次决定。"""
    request = _request(max_output_tokens=100, extra_params={"max_tokens": 999})

    assert build_payload(request, "m1")["max_tokens"] == 999


def test_the_configured_model_wins_over_the_request() -> None:
    request = _request(model="from-request")

    assert build_payload(request, "from-config")["model"] == "from-config"


def test_the_requests_own_model_is_the_fallback() -> None:
    request = _request(model="from-request")

    assert build_payload(request, "")["model"] == "from-request"


# --------------------------------------------------------------------------
# 响应体 → LlmResponse
# --------------------------------------------------------------------------


def _body(*, finish_reason: str = "", **message: JsonValue) -> JsonObject:
    """一个正常的回答。`**message` 往 `message` 里塞，不是往 choice 里塞。"""
    inner: JsonObject = {"content": "晴"}
    inner.update(message)
    choice: JsonObject = {"message": inner}
    if finish_reason:
        choice["finish_reason"] = finish_reason
    return {"choices": [choice], "usage": {"total_tokens": 12}}


def test_the_text_comes_back() -> None:
    assert parse_response(_body()).content == "晴"


def test_usage_comes_back() -> None:
    assert parse_response(_body()).usage == {"total_tokens": 12}


def test_a_missing_reasoning_stays_none() -> None:
    assert parse_response(_body()).reasoning is None


def test_reasoning_is_read_from_either_key() -> None:
    """各家叫法不一样，两个名字都认。"""
    assert parse_response(_body(reasoning_content="想了想")).reasoning == "想了想"
    assert parse_response(_body(reasoning="想了想")).reasoning == "想了想"


def test_the_finish_reason_is_translated() -> None:
    assert parse_response(_body(finish_reason="tool_calls")).finish_reason == (
        LlmFinishReason.TOOL_CALLS
    )


def test_an_unknown_finish_reason_falls_back_to_stop() -> None:
    """认不出的值当 stop 处理，不要把没见过的字符串塞进 core。"""
    assert parse_response(_body(finish_reason="weird")).finish_reason == LlmFinishReason.STOP


def test_no_usage_yields_an_empty_dict() -> None:
    body: JsonObject = {"choices": [{"message": {"content": "晴"}}]}

    assert parse_response(body).usage == {}


# --------------------------------------------------------------------------
# 响应体里的 tool calls
# --------------------------------------------------------------------------


def _tool_body(function: JsonObject, call_id: str = "t-1") -> JsonObject:
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [{"id": call_id, "type": "function", "function": function}],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


def test_tool_calls_are_flattened_back_out() -> None:
    response = parse_response(_tool_body({"name": "echo", "arguments": '{"text": "hi"}'}))

    call = response.tool_calls[0]
    assert call.tool == "echo"
    assert call.tool_call_id == "t-1"
    assert call.arguments == {"text": "hi"}


def test_empty_arguments_become_an_empty_object() -> None:
    """有些模型不调工具时给空串。"""
    response = parse_response(_tool_body({"name": "now", "arguments": ""}))

    assert response.tool_calls[0].arguments == {}


def test_broken_arguments_are_an_error() -> None:
    """当成空对象会让工具拿着静默错误的入参跑完，失败点落在工具里。"""
    with pytest.raises(LlmError):
        parse_response(_tool_body({"name": "echo", "arguments": "{不是 JSON"}))


def test_arguments_that_are_not_an_object_are_an_error() -> None:
    with pytest.raises(LlmError):
        parse_response(_tool_body({"name": "echo", "arguments": "[1, 2]"}))


def test_no_tool_calls_means_none() -> None:
    assert parse_response(_body()).tool_calls == ()


# --------------------------------------------------------------------------
# 形状不对
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param([], id="不是对象"),
        pytest.param({}, id="没有 choices"),
        pytest.param({"choices": []}, id="choices 是空的"),
        pytest.param({"choices": [{}]}, id="choice 里没有 message"),
    ],
)
def test_a_response_that_is_not_an_answer_is_an_error(body: JsonObject) -> None:
    """空 `LlmResponse` 会让 agent 以为模型说了空话，然后把这句空话落进历史。"""
    with pytest.raises(LlmError):
        parse_response(body)


def test_a_null_content_is_an_empty_string() -> None:
    """`content: null` 是模型要调工具时的常态，不该当成错。"""
    body: JsonObject = {"choices": [{"message": {"content": None}}]}

    assert parse_response(body).content == ""


# --------------------------------------------------------------------------
# complete
# --------------------------------------------------------------------------


def test_a_missing_api_key_is_refused_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 key 就不该发请求 —— 发了也只是换一个报错。"""
    monkeypatch.delenv("OATH_TEST_KEY", raising=False)
    provider = OpenAICompatibleProvider(
        ModelProviderConfig(model="m", api_base="http://x", api_key_env="OATH_TEST_KEY")
    )

    with pytest.raises(LlmError, match="OATH_TEST_KEY"):
        provider.complete(_request())


def test_an_empty_api_base_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OATH_TEST_KEY", "k")
    provider = OpenAICompatibleProvider(
        ModelProviderConfig(model="m", api_base="", api_key_env="OATH_TEST_KEY")
    )

    with pytest.raises(LlmError, match="api_base"):
        provider.complete(_request())


def test_wants_tools_reads_the_finish_reason() -> None:
    response = parse_response(_tool_body({"name": "echo", "arguments": "{}"}))

    assert response.wants_tools is True
    assert LlmResponse(content="晴").wants_tools is False
