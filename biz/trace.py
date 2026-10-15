"""runtime 暴露出来的观测数据，按 trace 取。

这一层只做一件事：把 `TraceStore` 里的 span 变成能直接 JSON 序列化的对象返回。
它不新增语义，也不改任何状态 —— 会话身份、输入接纳、状态快照都留在 `bootstrap`
的 `Runtime` 上，biz 只多管 trace 这一样。
"""

from __future__ import annotations

from core.protocol.datatype.scope import Scope
from core.protocol.datatype.trace import Span
from core.protocol.interface.trace import TraceStore
from core.utils.encoding import JsonObject, JsonValue


def list_traces(traces: TraceStore, scope: Scope) -> JsonObject:
    """这个 scope 的 trace 列表，每条带 span 数、step 数与累计耗时。

    累计耗时读的时候算而不是存下来：存了就是第二份真相，span 一改它就过期了。
    """
    listed: list[JsonValue] = []
    for trace_id in traces.trace_ids(scope):
        spans = traces.spans(scope, trace_id)
        listed.append(
            {
                "trace_id": trace_id,
                "span_count": len(spans),
                "step_count": sum(1 for span in spans if span.parent_span_id is None),
                "duration_ms": _elapsed(spans),
            }
        )
    return {"trace_ids": listed}


def get_trace(traces: TraceStore, scope: Scope, trace_id: str) -> JsonObject:
    """按 trace_id 取一条 trace 的全部 span。找不到就是空列表 —— 与「无权查看」不可区分。"""
    spans = traces.spans(scope, trace_id)
    return {
        "trace_id": trace_id,
        "duration_ms": _elapsed(spans),
        "spans": [{**span.to_dict(), "duration_ms": span.duration_ms} for span in spans],
    }


def _elapsed(spans: tuple[Span, ...]) -> float | None:
    """trace 的累计耗时：`max(end) - min(start)`。一条都没结束就是 `None`。"""
    ends = [span.end_unix_nano for span in spans if span.end_unix_nano is not None]
    if not ends:
        return None
    return round((max(ends) - min(span.start_unix_nano for span in spans)) / 1_000_000, 3)


__all__ = ["get_trace", "list_traces"]
