"""Render fixed input data without promoting event contents to instructions."""

from datetime import UTC, datetime
from xml.sax.saxutils import escape, quoteattr

from core.protocol.datatype.inbound import Inbound
from core.utils.encoding import canonical_json


def event_xml(item: Inbound, buffered_at=None) -> str:
    reference = item.payload.get("message_id") or item.source_id
    if item.source == "user.message":
        reference = "user:" + str(reference)
    at = item.occur_dt
    if not at and buffered_at is not None:
        at = datetime.fromtimestamp(buffered_at, UTC).isoformat()
    if item.source in {"user.message", "subagent.start"}:
        content = str(item.payload.get("content", ""))
    else:
        content = canonical_json({k: v for k, v in item.payload.items() if k != "dynamic"})
    dynamic = item.payload.get("dynamic")
    extra = (
        f"<context>{escape(canonical_json(dynamic))}</context>"
        if isinstance(dynamic, dict) and dynamic
        else ""
    )
    return (
        f"<event type={quoteattr(item.source)} id={quoteattr(str(reference))}>"
        f"<content>{escape(content)}</content>"
        f"<dynamic><time>{escape(at)}</time>{extra}</dynamic></event>"
    )
