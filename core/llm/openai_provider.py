"""`OpenAICompatibleProvider` —— OpenAI 兼容接口的非流式实现。

翻译两件事：core 的 provider 无关消息 → 请求体，请求体 → core 的 `LlmResponse`。
`ToolCallRef` 的扁平 `{id, name, arguments}` 在这里被包成 OpenAI 要的
`{id, type, function:{name, arguments}}`，`arguments` 从对象序列化成字符串 ——
反过来进 core 时再解析成对象。

**只支持 OpenAI 兼容这一种 wire。** 别的厂家要么提供兼容端点，要么自己写一个
`ModelProvider` 实现。core 不为某一个厂家开分支。
"""

from __future__ import annotations

import json
import os

import httpx

from core.protocol.datatype.config import ModelProviderConfig
from core.protocol.datatype.llm import (
    LlmFinishReason,
    LlmRequest,
    LlmResponse,
    LlmToolCall,
)
from core.protocol.datatype.message import Message
from core.protocol.error import LlmError
from core.protocol.interface.llm import ModelProvider
from core.utils.encoding import JsonObject, JsonValue

#: 端点后缀。`api_base` 可以写到完整 endpoint，写到 base 也认。
_CHAT_SUFFIX = "/chat/completions"

#: 请求/响应里那些名字各家不一样的字段 → core 的名字。
_REASONING_KEYS = ("reasoning_content", "reasoning")

_FINISH_REASONS = {
    "stop": LlmFinishReason.STOP,
    "length": LlmFinishReason.LENGTH,
    "tool_calls": LlmFinishReason.TOOL_CALLS,
    "content_filter": LlmFinishReason.CONTENT_FILTER,
}


def _tool_call_refs(message: Message) -> list[JsonValue]:
    """`Message.extra["tool_calls"]` → OpenAI 的数组形状。

    没有就返回空列表，不是错：一次没有工具调用的 assistant 消息就是没有。
    """
    raw = message.extra.get("tool_calls")
    if not isinstance(raw, list):
        return []
    refs: list[JsonValue] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        refs.append(
            {
                "id": item.get("id", ""),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": json.dumps(item.get("arguments", {}), ensure_ascii=False),
                },
            }
        )
    return refs


def to_wire_message(message: Message) -> JsonObject:
    """一条 `Message` → 请求体里的一条。"""
    out: JsonObject = {"role": message.role, "content": message.content}
    for key, value in message.extra.items():
        if key == "tool_calls":
            continue
        # `outcome` / `unresolved` 是 Oath 给投影与客户端看的标记，
        # 模型 API 不认 —— 发出去会被拒或者被静默忽略。
        if key in ("outcome", "unresolved", "model", "usage"):
            continue
        out[key] = value
    calls = _tool_call_refs(message)
    if calls:
        out["tool_calls"] = calls
    return out


def to_wire_tools(request: LlmRequest) -> list[JsonValue] | None:
    """`ToolDefinition` → 请求体里的 `tools`。

    一个工具都没有时返回 `None`：有些厂家见到空数组会报错，给 `None` 是各家都认的
    「这轮没有工具」。
    """
    if not request.tools:
        return None
    tools: list[JsonValue] = [
        {
            "type": "function",
            "function": {
                "name": definition.name,
                "description": definition.description,
                "parameters": definition.input_schema,
            },
        }
        for definition in request.tools
    ]
    return tools


def build_payload(request: LlmRequest, model: str) -> JsonObject:
    """整个请求体。

    合并顺序（后者覆盖前者）：provider 透传参数 → 请求自带参数 → 调用点显式给的
    两个（model / max_tokens / temperature）。显式给的赢 —— 那是最具体的一次决定。
    """
    payload: JsonObject = {
        "model": model or request.model,
        "messages": [to_wire_message(m) for m in request.messages],
        "stream": False,
        "max_tokens": request.max_output_tokens,
    }
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    tools = to_wire_tools(request)
    if tools is not None:
        payload["tools"] = tools
    payload.update(request.extra_params)
    return payload


def _api_key(config: ModelProviderConfig) -> str:
    return os.environ.get(config.api_key_env, "")


def parse_response(body: JsonValue) -> LlmResponse:
    """响应体 → `LlmResponse`。

    形状不对一律抛 `LlmError`：一个空 `LlmResponse` 会让 agent 以为模型说了空话，
    然后把这段空话落进对话历史 —— 那是静默的坏数据。
    """
    if not isinstance(body, dict):
        raise LlmError(f"响应体必须是对象，得到 {type(body).__name__}")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmError("响应里没有 choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise LlmError("choices[0] 必须是对象")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise LlmError("choices[0].message 必须是对象")

    reasoning: str | None = None
    for key in _REASONING_KEYS:
        value = message.get(key)
        if isinstance(value, str) and value:
            reasoning = value
            break

    usage = body.get("usage")
    finish = choice.get("finish_reason")
    return LlmResponse(
        content=_as_text(message.get("content")),
        reasoning=reasoning,
        tool_calls=_parse_tool_calls(message.get("tool_calls")),
        finish_reason=_FINISH_REASONS.get(finish, LlmFinishReason.STOP)
        if isinstance(finish, str)
        else LlmFinishReason.STOP,
        usage=dict(usage) if isinstance(usage, dict) else {},
    )


def _as_text(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""


def _parse_tool_calls(raw: JsonValue) -> tuple[LlmToolCall, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise LlmError(f"tool_calls 必须是数组，得到 {type(raw).__name__}")
    calls: list[LlmToolCall] = []
    for item in raw:
        if not isinstance(item, dict):
            raise LlmError(f"tool_calls 里的项必须是对象，得到 {type(item).__name__}")
        function = item.get("function")
        if not isinstance(function, dict):
            raise LlmError("tool_calls[].function 必须是对象")
        calls.append(
            LlmToolCall(
                tool=_as_text(function.get("name")),
                arguments=_parse_arguments(function.get("arguments")),
                tool_call_id=_as_text(item.get("id")),
            )
        )
    return tuple(calls)


def _parse_arguments(raw: JsonValue) -> JsonObject:
    """`arguments` 从 JSON 字符串变回对象。

    解析失败抛错，**不当成空对象**：空参数会让工具拿着一个静默错误的入参跑完，
    失败点落在工具里，看不出是这里出的问题。
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise LlmError(f"arguments 必须是 JSON 字符串，得到 {type(raw).__name__}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LlmError(f"arguments 不是合法 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LlmError(f"arguments 解析出来必须是对象，得到 {type(parsed).__name__}")
    return parsed


class OpenAICompatibleProvider(ModelProvider):
    """一次同步的 chat/completions 调用。"""

    def __init__(self, config: ModelProviderConfig) -> None:
        self._config = config

    def complete(self, request: LlmRequest) -> LlmResponse:
        key = _api_key(self._config)
        if not key:
            raise LlmError(f"环境变量 {self._config.api_key_env} 没设置，取不到 api key")
        base = self._config.api_base.rstrip("/")
        if base.endswith(_CHAT_SUFFIX):
            base = base[: -len(_CHAT_SUFFIX)]
        if not base:
            raise LlmError("api_base 为空")

        timeout = httpx.Timeout(self._config.timeout_seconds, connect=10.0)
        try:
            response = httpx.post(
                f"{base}{_CHAT_SUFFIX}",
                json=build_payload(request, self._config.model),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise LlmError(f"请求失败: {exc}") from exc

        if response.status_code >= 400:
            raise LlmError(f"模型返回 {response.status_code}: {response.text[:500]}")
        try:
            body = response.json()
        except ValueError as exc:
            raise LlmError(f"响应不是 JSON: {exc}") from exc
        return parse_response(body)


__all__ = [
    "OpenAICompatibleProvider",
    "build_payload",
    "parse_response",
    "to_wire_message",
    "to_wire_tools",
]
