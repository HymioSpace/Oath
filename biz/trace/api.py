"""runtime 暴露出来的观测数据，按 trace 取。

这一层只做一件事：把 `TraceStore` 里的 span 变成能直接 JSON 序列化的对象返回。
它不新增语义，也不改任何状态 —— 会话身份、输入接纳、状态快照都留在 `bootstrap`
的 `Runtime` 上。

两个函数只吃 `TraceStore`，不认识 `Runtime`：换存储后端不用改调用方，也不会因为
runtime 装配变了就把这里一起拖下水。
"""

from __future__ import annotations

from core.protocol.datatype.trace import Span
from core.protocol.datatype.user import User
from core.protocol.interface.trace import TraceStore
from core.utils.encoding import JsonObject, JsonValue


def list_traces(traces: TraceStore, user: User, *, limit: int, offset: int) -> JsonObject:
    """这个 user 的 trace 列表的一页，每条带 span 数、step 数与累计耗时。

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

    `summary` 保留节点、时间、状态、属性名和事件名，但不返回模型请求/响应等
    event 属性正文。移动端先读 summary，只有用户要求原始数据时才读取 full。
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
                "attributes": {} if summary else event.attributes,
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


def _elapsed(spans: tuple[Span, ...]) -> float | None:
    """trace 的累计耗时：`max(end) - min(start)`。一条都没结束就是 `None`。"""
    ends = [span.end_unix_nano for span in spans if span.end_unix_nano is not None]
    if not ends:
        return None
    return round((max(ends) - min(span.start_unix_nano for span in spans)) / 1_000_000, 3)


__all__ = ["list_traces", "query_trace"]
