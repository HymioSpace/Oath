"""工具终态的判定与对外表示；调用方在事务提交后把终态投进 tool 队列。"""

from __future__ import annotations

from typing import Any

from core.detail.shrink import Detail
from core.journal.runtime_store import State
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import QueueName
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
) -> Inbound | None:
    """把一次调用终结，返回可以投进 tool 队列的那条输入。已经终结过就返回 None。

    终态本身在 journal 里；返回的这一条只是它的对外表示，**发布由调用方在事务之外做** ——
    队列里不能出现 journal 里还没有的事实。
    """
    if call["status"] in TERMINAL:
        return None
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
    return Inbound(
        kind=QueueName.TOOL,
        source=ToolEventType.TERMINAL,
        source_id=str(payload["event_id"]),
        payload=payload,
        occur_dt=call["finished_dt"],
    )
