"""runtime 暴露出来的观测数据，按 trace 取。

这一层只做一件事：把 `TraceStore` 里的 span 变成能直接 JSON 序列化的对象返回。
它不新增语义，也不改任何状态 —— 会话身份、输入接纳、状态快照都留在 `bootstrap`
的 `Runtime` 上。

两个函数只吃 `TraceStore`，不认识 `Runtime`：换存储后端不用改调用方，也不会因为
runtime 装配变了就把这里一起拖下水。
"""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any

from core.protocol.datatype.trace import Span
from core.protocol.datatype.user import User
from core.protocol.interface.trace import TraceStore
from core.utils.encoding import JsonObject, JsonValue

_TITLE_LIMIT = 72
_MESSAGE_LIMIT = 24
_MESSAGE_CHARS = 2_000
_TEXT_CHARS = 8_000


def list_traces(traces: TraceStore, user: User, *, limit: int, offset: int) -> JsonObject:
    """这个 user 的 trace 列表的一页，每条带可读标题、开始时间与执行摘要。

    排序按最近写入，页内是 `offset` 起的一截。`total` 是全部条数，客户端靠它判断还有
    没有下一页。分页只切列表 —— 一条 trace 的 span 数、耗时都是整条读的，不跨页。
    """
    trace_ids = traces.trace_ids(user)
    listed: list[JsonValue] = []
    for trace_id in trace_ids[offset : offset + limit]:
        spans = traces.spans(user, trace_id)
        listed.append(
            {
                "trace_id": trace_id,
                "title": _trace_title(spans),
                "started_at": _started_at(spans),
                "span_count": len(spans),
                "step_count": sum(1 for span in spans if span.parent_span_id is None),
                "duration_ms": _elapsed(spans),
            }
        )
    return {"trace_ids": listed, "total": len(trace_ids), "limit": limit, "offset": offset}


def query_trace(
    traces: TraceStore, user: User, trace_id: str, *, summary: bool = False
) -> JsonObject:
    """按 trace_id 取一条 trace。

    `summary` 保留节点、时间、状态、属性名，以及有界的模型请求/响应和工具事件正文。
    完整 payload 只在用户明确读取原始数据时返回。这样移动端首屏能解释模型收到了什么、
    又返回了什么，同时不会把整段工具输出一次性塞进界面。
    """
    spans = traces.spans(user, trace_id)
    return {
        "trace_id": trace_id,
        "duration_ms": _elapsed(spans),
        "view": "summary" if summary else "full",
        "spans": [_span(span, summary=summary) for span in spans],
    }


def _span(span: Span, *, summary: bool) -> JsonObject:
    result: JsonObject = {
        "trace_id": span.trace_id,
        "span_id": span.span_id,
        "parent_span_id": span.parent_span_id,
        "name": span.name,
        "kind": span.kind,
        "start_unix_nano": span.start_unix_nano,
        "end_unix_nano": span.end_unix_nano,
        "status": span.status,
        "duration_ms": span.duration_ms,
        "attributes": span.attributes,
        "events": [
            {
                "name": event.name,
                "unix_nano": event.unix_nano,
                "attributes": (
                    _summary_event_attributes(event.name, event.attributes)
                    if summary
                    else event.attributes
                ),
            }
            for event in span.events
        ],
        "links": [
            {
                "trace_id": link.trace_id,
                "span_id": link.span_id,
                "attributes": {} if summary else link.attributes,
            }
            for link in span.links
        ],
    }
    return result


def _summary_event_attributes(name: str, attributes: JsonObject) -> JsonObject:
    """Freeze an event payload into a small, still-readable wire shape."""
    result: JsonObject = {}
    for key, value in attributes.items():
        if key == "body":
            continue
        result[key] = _bounded(value, depth=0)
    if "body" not in attributes:
        result["summary"] = True
        return result
    raw = attributes["body"]
    payload = _decode_body(raw)
    summarized = _summarize_body(name, payload)
    result["body"] = (
        json.dumps(summarized, ensure_ascii=False, separators=(",", ":"))
        if isinstance(raw, str)
        else summarized
    )
    result["summary"] = True
    return result


def _summarize_body(name: str, payload: Any) -> Any:
    if not isinstance(payload, dict):
        return _bounded(payload, depth=0)
    if name == "gen_ai.request" and isinstance(payload.get("messages"), list):
        messages = payload["messages"]
        selected: list[JsonValue] = []
        # Preserve the setup and the latest turn. The full transcript remains behind the
        # explicit "raw attributes" action.
        indexes = list(range(min(4, len(messages))))
        remaining = _MESSAGE_LIMIT - len(indexes)
        indexes.extend(range(max(0, len(messages) - remaining), len(messages)))
        for index in sorted(dict.fromkeys(indexes)):
            message = messages[index]
            if not isinstance(message, dict):
                selected.append(_bounded(message, depth=0))
                continue
            summarized_message: JsonObject = {}
            for key, value in message.items():
                if key == "content":
                    summarized_message[key] = _bounded_content(value)
                elif key == "tool_calls":
                    summarized_message[key] = _bounded(value, depth=0, items=12)
                else:
                    summarized_message[key] = _bounded(value, depth=0, chars=512)
            selected.append(summarized_message)
        request = {
            key: _bounded(value, depth=0, items=24)
            for key, value in payload.items()
            if key != "messages"
        }
        request["message_count"] = len(messages)
        request["messages_truncated"] = len(selected) < len(messages)
        request["messages"] = selected
        return request
    if name == "gen_ai.response":
        response = {
            key: _bounded(value, depth=0, items=16)
            for key, value in payload.items()
            if key not in {"content", "tool_calls"}
        }
        if "content" in payload:
            response["content"] = _bounded_content(payload["content"])
        calls = payload.get("tool_calls")
        if isinstance(calls, list):
            response["tool_call_count"] = len(calls)
            response["tool_calls_truncated"] = len(calls) > 12
            response["tool_calls"] = [_bounded(call, depth=0, items=16) for call in calls[:12]]
        return response
    return _bounded(payload, depth=0)


def _bounded(
    value: Any,
    *,
    depth: int,
    chars: int = _TEXT_CHARS,
    items: int = 40,
) -> JsonValue:
    if depth > 4:
        return _text(value, chars)
    if isinstance(value, str):
        return _text(value, chars)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        return [_bounded(item, depth=depth + 1, chars=chars, items=items) for item in value[:items]]
    if isinstance(value, dict):
        return {
            str(key): _bounded(item, depth=depth + 1, chars=chars, items=items)
            for key, item in list(value.items())[:items]
        }
    return _text(value, chars)


def _bounded_content(value: Any) -> JsonValue:
    if isinstance(value, list):
        result: list[JsonValue] = []
        for part in value[:16]:
            if isinstance(part, dict):
                kind = str(part.get("type") or "")
                if kind == "image_url":
                    result.append({"type": "image_url", "image_url": "[图片已省略]"})
                    continue
                result.append(_bounded(part, depth=0, chars=_MESSAGE_CHARS, items=16))
            else:
                result.append(_bounded(part, depth=0, chars=_MESSAGE_CHARS))
        return result
    return _bounded(value, depth=0, chars=_MESSAGE_CHARS)


def _text(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _decode_body(raw: JsonValue) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw
    return raw


def _body_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for part in value:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        for key in ("payload", "content", "text", "message", "preview"):
            if key in value:
                text = _body_text(value[key])
                if text:
                    return text
    return ""


def _trace_title(spans: tuple[Span, ...]) -> str:
    events = sorted(
        (event for span in spans for event in span.events),
        key=lambda event: event.unix_nano,
    )
    for event in events:
        if event.name != "oath.input.user":
            continue
        title = _readable_title_text(
            _body_text(_decode_body(event.attributes.get("body")))
        )
        if title:
            return title
    for event in events:
        if event.name != "gen_ai.request":
            continue
        payload = _decode_body(event.attributes.get("body"))
        if not isinstance(payload, dict):
            continue
        messages = payload.get("messages")
        if not isinstance(messages, list):
            continue
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            title = _readable_title_text(_body_text(message.get("content")))
            if title:
                return title
    for event in events:
        if not event.name.startswith("oath.input."):
            continue
        title = _system_input_title(_decode_body(event.attributes.get("body")))
        if title:
            return title
    return "系统执行记录"


def _title_text(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= _TITLE_LIMIT:
        return text
    return text[: _TITLE_LIMIT - 1].rstrip() + "…"


def _readable_title_text(value: str) -> str:
    title = _title_text(value)
    if not title or title.startswith(("{", "[", "<")):
        return ""
    return title


def _system_input_title(value: Any) -> str:
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                return _system_input_title(json.loads(stripped))
            except (TypeError, ValueError):
                return ""
        event_type = re.search(r'<event\s+type="([^"]+)"', value)
        if event_type:
            return _event_type_title(event_type.group(1))
        content = re.search(r"<content>(.*?)</content>", value, flags=re.DOTALL)
        if content:
            text = re.sub(r"<[^>]+>", " ", unescape(content.group(1)))
            return _readable_title_text(text)
        return ""
    if not isinstance(value, dict):
        return ""
    payload = value.get("payload")
    if isinstance(payload, dict):
        for key in ("title", "summary", "message", "content"):
            title = _readable_title_text(_body_text(payload.get(key)))
            if title:
                return title
        event_type = payload.get("event_type") or payload.get("type")
        if isinstance(event_type, str) and event_type:
            return _event_type_title(event_type)
    for key in ("event_type", "type", "source"):
        event_type = value.get(key)
        if isinstance(event_type, str) and event_type:
            return _event_type_title(event_type)
    return _readable_title_text(_body_text(payload))


def _event_type_title(value: str) -> str:
    labels = {
        "app.task.completed": "后台任务已完成",
        "app.task.workflow_ready": "任务工作流已就绪",
        "app.task.budget_exhausted": "任务预算已用尽",
        "app.task.blocked": "任务需要处理",
        "app.task.failed": "后台任务未完成",
    }
    if value in labels:
        return labels[value]
    return _title_text(value.replace(".", " · ").replace("_", " "))


def _started_at(spans: tuple[Span, ...]) -> float | None:
    if not spans:
        return None
    starts = [span.start_unix_nano for span in spans]
    starts.extend(event.unix_nano for span in spans for event in span.events)
    return round(min(starts) / 1_000_000_000, 3)


def _elapsed(spans: tuple[Span, ...]) -> float | None:
    """trace 的累计耗时：`max(end) - min(start)`。一条都没结束就是 `None`。"""
    ends = [span.end_unix_nano for span in spans if span.end_unix_nano is not None]
    if not ends:
        return None
    return round((max(ends) - min(span.start_unix_nano for span in spans)) / 1_000_000, 3)


__all__ = ["list_traces", "query_trace"]
