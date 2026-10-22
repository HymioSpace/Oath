"""Best-effort cross-service span writes over the existing Storage contract."""

import logging

from core.protocol.datatype.trace import Span, SpanKind, SpanStatus, new_span_id
from core.protocol.interface.storage import StoragePort
from core.utils.encoding import JsonObject

_log = logging.getLogger(__name__)


def append_span(
    storage: StoragePort,
    *,
    user_id: str,
    trace_id: str,
    name: str,
    started_ns: int,
    ended_ns: int,
    attributes: JsonObject,
    status: str = SpanStatus.OK,
) -> None:
    if len(trace_id) != 32:
        return
    try:
        span = Span(
            trace_id=trace_id,
            span_id=new_span_id(),
            name=name,
            kind=SpanKind.INTERNAL,
            start_unix_nano=started_ns,
            end_unix_nano=ended_ns,
            status=status,
            attributes=attributes,
        )
        storage.call("trace.append", user_id=user_id, span=span.to_dict())
    except Exception:
        _log.warning("Cross-service trace write failed for %s", name)


__all__ = ["append_span"]
