"""trace 的读模型：列出一个 scope 的 trace，取出其中一条。

只依赖 `TraceStore`，不认识 runtime —— 读模型可以被别的传输复用，也不用拿整个
装配来测。跨 scope 的可见性由 store 保证，这里不再检查一遍。
"""

from __future__ import annotations

from biz.trace.view import elapsed, view
from core.protocol.datatype.scope import Scope
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
                "duration_ms": elapsed(spans),
            }
        )
    return {"trace_ids": listed}


def get_trace(traces: TraceStore, scope: Scope, trace_id: str) -> JsonObject:
    """一条 trace 的全部 span。找不到就是空列表 —— 与「无权查看」不可区分。"""
    spans = traces.spans(scope, trace_id)
    return {"trace_id": trace_id, "duration_ms": elapsed(spans), "spans": view(spans)}


__all__ = ["get_trace", "list_traces"]
