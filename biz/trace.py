"""trace 的读模型：列出一个 scope 的 trace，取出其中一条。

只依赖 `TraceStore`，不认识 runtime —— 这样读模型可以被别的传输复用，也不用拿整个
装配来测。跨 scope 的可见性由 store 保证，这里不做第二道检查。
"""

from __future__ import annotations

from core.protocol.datatype.scope import Scope
from core.protocol.datatype.trace import Span
from core.protocol.interface.trace import TraceStore
from core.utils.encoding import JsonObject, JsonValue


def list_traces(traces: TraceStore, scope: Scope) -> JsonObject:
    """这个 scope 的 trace 列表，每条带 span 数、step 数与累计耗时。

    累计耗时在这里算而不是存下来：存了就是第二份真相，span 一改它就过期了。
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
    """一条 trace 的全部 span。找不到就是空列表 —— 与「无权查看」不可区分。"""
    spans = traces.spans(scope, trace_id)
    return {"trace_id": trace_id, "duration_ms": _elapsed(spans), "spans": _view(spans)}


def _view(spans: tuple[Span, ...]) -> list[JsonValue]:
    return [{**span.to_dict(), "duration_ms": span.duration_ms} for span in spans]


def _elapsed(spans: tuple[Span, ...]) -> float | None:
    """trace 的累计耗时：`max(end) - min(start)`。没有已结束的 span 就是 `None`。"""
    ends = [span.end_unix_nano for span in spans if span.end_unix_nano is not None]
    if not ends:
        return None
    return round((max(ends) - min(span.start_unix_nano for span in spans)) / 1_000_000, 3)


__all__ = ["get_trace", "list_traces"]
