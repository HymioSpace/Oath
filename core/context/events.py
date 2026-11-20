"""Render fixed input data without promoting event contents to instructions."""

from datetime import UTC, datetime
from xml.sax.saxutils import escape, quoteattr
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.protocol.datatype.inbound import Inbound
from core.utils.encoding import canonical_json


def _client_timezone(item: Inbound, fallback: str) -> str:
    dynamic = item.payload.get("dynamic")
    user_context = item.payload.get("user_context")
    candidates = (
        dynamic.get("time_zone") if isinstance(dynamic, dict) else None,
        dynamic.get("timezone") if isinstance(dynamic, dict) else None,
        user_context.get("time_zone") if isinstance(user_context, dict) else None,
        fallback,
    )
    return next((value for value in candidates if isinstance(value, str) and value), "")


def _local_time(value: str, timezone: str) -> str:
    if not value or not timezone:
        return value
    try:
        zone = ZoneInfo(timezone)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, ZoneInfoNotFoundError):
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(zone).isoformat()


def event_xml(item: Inbound, buffered_at=None, client_timezone: str = "") -> str:
    reference = item.payload.get("message_id") or item.source_id
    if item.source == "user.message":
        reference = "user:" + str(reference)
    at = item.occur_dt
    if not at and buffered_at is not None:
        at = datetime.fromtimestamp(buffered_at, UTC).isoformat()
    if item.source in {"user.message", "subagent.start", "subagent.message"}:
        content = str(item.payload.get("content", ""))
    else:
        content = canonical_json({k: v for k, v in item.payload.items() if k != "dynamic"})
    timezone = _client_timezone(item, client_timezone)
    at = _local_time(at, timezone)
    dynamic = item.payload.get("dynamic")
    context = dict(dynamic) if isinstance(dynamic, dict) else {}
    if timezone and not any(key in context for key in ("time_zone", "timezone")):
        context["time_zone"] = timezone
    extra = (
        f"<context>{escape(canonical_json(context))}</context>" if context else ""
    )
    return (
        f"<event type={quoteattr(item.source)} id={quoteattr(str(reference))}>"
        f"<content>{escape(content)}</content>"
        f"<dynamic><time>{escape(at)}</time>{extra}</dynamic></event>"
    )
