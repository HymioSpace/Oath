"""工具终态与可靠输入在同一事务中提交；调用方不能二次终结。"""

from __future__ import annotations

from typing import Any

from core.buffer.journal_buffer import accept_input
from core.detail.shrink import Detail
from core.journal.runtime_store import State
from core.protocol.datatype.inbound import Inbound, InboundKind
from core.protocol.datatype.tool import ToolEventType
from core.utils.encoding import utc_now_iso

TERMINAL = frozenset({"succeeded", "failed", "timeout", "cancelled", "outcome_unknown"})
MAX_ERROR_MESSAGE_LENGTH = 500


def terminal(
    state: State,
    call: State,
    outcome: str,
    *,
    content: Any = None,
    error: str | None = None,
    error_kind: str | None = None,
    detail: Detail | None = None,
) -> None:
    if call["status"] in TERMINAL:
        return
    call.update(status=outcome, finished_dt=utc_now_iso())
    payload = {
        "invocation_id": call["invocation_id"],
        "tool_call_id": call["tool_call_id"],
        "batch_id": call["batch_id"],
        "run_id": call["run_id"],
        "source": call["source"],
        "event_id": f"result:{call['invocation_id']}",
        "outcome": outcome,
    }
    if error is None:
        payload["content"] = content
        if detail is not None:
            payload["detail_key"] = detail.key
            payload["original_chars"] = detail.original_chars
    else:
        payload["error"] = {
            "kind": error_kind or outcome,
            "message": error[:MAX_ERROR_MESSAGE_LENGTH],
        }
    call["terminal"] = payload
    accept_input(
        state,
        Inbound(
            kind=InboundKind.TOOL_RESULT,
            source=ToolEventType.TERMINAL,
            source_id=payload["event_id"],
            payload=payload,
            occur_dt=call["finished_dt"],
        ),
    )
