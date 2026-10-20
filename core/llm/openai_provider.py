"""`OpenAICompatibleProvider` —— OpenAI 兼容接口的实现。

翻译两件事：core 的 provider 无关消息 → 请求体，请求体 → core 的 `LlmResponse`。
`ToolCallRef` 的扁平 `{id, name, arguments}` 在这里被包成 OpenAI 要的
`{id, type, function:{name, arguments}}`，`arguments` 从对象序列化成字符串 ——
反过来进 core 时再解析成对象。

**流式与不流式是两条路，返回值一样。** `on_delta` 给了就把 SSE 逐块攒起来，
`content` 与 `reasoning` 边攒边交给回调，`tool_calls` 攒完整再解析；攒完返回一个
和 `stream=false` 形状完全一致的 `LlmResponse`。所以调用方拿到之后做的事 —— 落盘、
派发工具、收尾 —— 两种模式下是同一段代码，没有分叉。

**只支持 OpenAI 兼容这一种 wire。** 别的厂家要么提供兼容端点，要么自己写一个
`ModelProvider` 实现。core 不为某一个厂家开分支。
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field

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
from core.protocol.interface.llm import DeltaChannel, DeltaSink, ModelProvider
from core.utils.encoding import JsonObject, JsonValue

#: 端点后缀。`api_base` 可以写到完整 endpoint，写到 base 也认。
_CHAT_SUFFIX = "/chat/completions"

#: SSE 的行前缀与结束哨兵。
_SSE_PREFIX = "data:"
_SSE_DONE = "[DONE]"

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


def build_payload(request: LlmRequest, model: str, *, stream: bool = False) -> JsonObject:
    """整个请求体。

    合并顺序（后者覆盖前者）：provider 透传参数 → 请求自带参数 → 调用点显式给的
    两个（model / max_tokens / temperature）。显式给的赢 —— 那是最具体的一次决定。

    `stream` 放在 `extra_params` **之前**：provider 透传参数里写 `stream = true` 不应该
    把调用点的选择顶掉 —— 要流式是调用点的决定。
    """
    payload: JsonObject = {
        "model": model or request.model,
        "messages": [to_wire_message(m) for m in request.messages],
        "max_tokens": request.max_output_tokens,
    }
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    tools = to_wire_tools(request)
    if tools is not None:
        payload["tools"] = tools
    payload.update(request.extra_params)
    payload["stream"] = stream
    return payload


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


@dataclass(slots=True)
class _PartialCall:
    """一个还在攒的 tool call。

    `arguments` 存**片段**而不是拼好的字符串：一边收一边拼的话，某一轮里收到
    非字符串（null、对象）就会把整个缓冲区弄成坏类型，错误还会留到最后才暴露。
    """

    tool_call_id: str = ""
    name: str = ""
    fragments: list[str] = field(default_factory=list)


class _StreamAccumulator:
    """把一串 SSE chunk 攒成一个 `LlmResponse`。

    **`tool_calls` 按 `index` 归并。** 各家切得不一样：OpenAI 会把一个调用的
    `function.arguments` 切成好几段慢慢发，MiniMax 一个 chunk 整段发完。两种都要收成
    一个调用 —— 所以靠 `index` 认谁是谁，不靠顺序，也不靠「上一条还没结束」这种推断。
    """

    def __init__(self, on_delta: DeltaSink | None) -> None:
        self._on_delta = on_delta
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._calls: dict[int, _PartialCall] = {}
        self._order: list[int] = []
        self._finish = LlmFinishReason.STOP
        self._seen_choice = False
        self._usage: JsonObject = {}

    def feed(self, chunk: JsonValue) -> None:
        if not isinstance(chunk, dict):
            raise LlmError(f"chunk 必须是对象，得到 {type(chunk).__name__}")
        error = chunk.get("error")
        if isinstance(error, dict):
            # 各家在流里报错的形状不统一，但都带一个能给人看的 message。报错的
            # 流后面照样会发 `[DONE]` —— 不当场认出来，一段空白的正常结束就会变成
            # 一条「模型什么也没说」的回答，然后被落进对话历史。
            message = error.get("message")
            raise LlmError(f"模型在流里报错: {message if message is not None else error}")
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self._usage = usage
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            # usage-only 的 chunk 是各家都发的，不能当错。
            return
        choice = choices[0]
        if not isinstance(choice, dict):
            raise LlmError("choices[0] 必须是对象")
        self._seen_choice = True
        finish = choice.get("finish_reason")
        if isinstance(finish, str) and finish in _FINISH_REASONS:
            self._finish = _FINISH_REASONS[finish]
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return
        self._take_text(delta, "content", self._content, DeltaChannel.CONTENT)
        for key in _REASONING_KEYS:
            if key in delta:
                self._take_text(delta, key, self._reasoning, DeltaChannel.REASONING)
                break
        self._take_calls(delta.get("tool_calls"))

    def result(self) -> LlmResponse:
        """攒完的结果。**一个 `choices` 都没出现过，判成失败。**

        断掉的流会以 `[DONE]` 收尾，各家都这么发，所以光看结束标记看不出问题。而一个
        空的 `LlmResponse` 会让 agent 以为模型说了空话，然后把这段空话落进对话历史
        —— 那是静默的坏数据，比一次明明白白的失败难查得多。

        只判「有没有 choices」，不判「收没收尾」：各家收尾的形状并不一致，而一个已经
        带着 `choices` 的 chunk 说明请求确实被受理了。
        """
        if not self._seen_choice:
            raise LlmError("流里没有任何 choices")
        return LlmResponse(
            content="".join(self._content),
            # 「一截都没有」与「有一段但是空串」在 journal 里长得一样，所以
            # 一段都没有就报 None，与非流式路径保持一致。
            reasoning="".join(self._reasoning) or None,
            tool_calls=self._calls_in_order(),
            finish_reason=self._finish,
            usage=dict(self._usage),
        )

    # -- 内部 ---------------------------------------------------------------

    def _take_text(self, delta: JsonObject, key: str, bucket: list[str], channel: str) -> None:
        text = _as_text(delta.get(key))
        if not text:
            return
        bucket.append(text)
        self._emit(channel, text)

    def _take_calls(self, raw: JsonValue) -> None:
        if not isinstance(raw, list):
            return
        for item in raw:
            if not isinstance(item, dict):
                raise LlmError(f"tool_calls 里的项必须是对象，得到 {type(item).__name__}")
            # 没有 index 就只有一个调用 —— 一条流里没有第二处可归。
            index = item.get("index")
            if not isinstance(index, int):
                index = 0
            entry = self._calls.get(index)
            if entry is None:
                entry = _PartialCall()
                self._calls[index] = entry
                self._order.append(index)
            # id 与 name 只在第一段里给；后来的段只有 arguments。
            # 用「非空才覆盖」而不是「有就覆盖」：有的厂家每段都重复带一遍。
            call_id = item.get("id")
            if isinstance(call_id, str) and call_id:
                entry.tool_call_id = call_id
            function = item.get("function")
            if isinstance(function, dict):
                name = function.get("name")
                if isinstance(name, str) and name:
                    entry.name = name
                entry.fragments.append(_as_text(function.get("arguments")))

    def _calls_in_order(self) -> tuple[LlmToolCall, ...]:
        return tuple(
            LlmToolCall(
                tool=self._calls[i].name,
                arguments=_parse_arguments("".join(self._calls[i].fragments)),
                tool_call_id=self._calls[i].tool_call_id,
            )
            for i in self._order
        )

    def _emit(self, channel: str, text: str) -> None:
        if self._on_delta is None:
            return
        # 送不出去的一段增量不是模型调用失败 —— 返回值仍然是完整的那一份。
        with contextlib.suppress(Exception):
            self._on_delta(channel, text)


class OpenAICompatibleProvider(ModelProvider):
    """一次 chat/completions 调用。`on_delta` 给了就走流式。"""

    def __init__(self, config: ModelProviderConfig, secrets=None) -> None:
        self._config = config
        self._secrets = secrets

    def complete(self, request: LlmRequest, *, on_delta: DeltaSink | None = None) -> LlmResponse:
        if not self._secrets or not self._config.credential:
            raise LlmError("Model credential is not configured")
        try:
            key = self._secrets.get(self._config.credential)
        except Exception as exc:
            raise LlmError("Cannot read model credential from Vault") from exc
        base = self._config.api_base.rstrip("/")
        if base.endswith(_CHAT_SUFFIX):
            base = base[: -len(_CHAT_SUFFIX)]
        if not base:
            raise LlmError("api_base 为空")

        payload = build_payload(
            request, request.model or self._config.model, stream=on_delta is not None
        )
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        timeout = httpx.Timeout(self._config.timeout_seconds, connect=10.0)
        if on_delta is None:
            return self._complete_once(f"{base}{_CHAT_SUFFIX}", payload, headers, timeout)
        return self._complete_streamed(f"{base}{_CHAT_SUFFIX}", payload, headers, timeout, on_delta)

    def _complete_once(
        self,
        url: str,
        payload: JsonObject,
        headers: dict[str, str],
        timeout: httpx.Timeout,
    ) -> LlmResponse:
        try:
            response = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise LlmError(f"请求失败: {exc}") from exc

        if response.status_code >= 400:
            raise LlmError(f"模型返回 {response.status_code}: {response.text[:500]}")
        try:
            body = response.json()
        except ValueError as exc:
            raise LlmError(f"响应不是 JSON: {exc}") from exc
        return parse_response(body)

    def _complete_streamed(
        self,
        url: str,
        payload: JsonObject,
        headers: dict[str, str],
        timeout: httpx.Timeout,
        on_delta: DeltaSink,
    ) -> LlmResponse:
        accumulator = _StreamAccumulator(on_delta)
        try:
            with httpx.stream("POST", url, json=payload, headers=headers, timeout=timeout) as resp:
                if resp.status_code >= 400:
                    raise LlmError(f"模型返回 {resp.status_code}: {resp.read()[:500]!r}")
                for line in resp.iter_lines():
                    if not line.startswith(_SSE_PREFIX):
                        continue
                    data = line[len(_SSE_PREFIX) :].strip()
                    if data == _SSE_DONE:
                        break
                    if not data:
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise LlmError(f"chunk 不是合法 JSON: {exc}") from exc
                    accumulator.feed(chunk)
        except httpx.HTTPError as exc:
            raise LlmError(f"请求失败: {exc}") from exc
        return accumulator.result()


__all__ = [
    "OpenAICompatibleProvider",
    "build_payload",
    "parse_response",
    "to_wire_message",
    "to_wire_tools",
]
