"""只读命令：查得到但不改变任何状态的那一半。

命令词表在这里，传输层不认识 command type —— 加一条查询 command 只改这个文件，
不牵动 WebSocket 那一层。查询回的是 event 帧而不是 ACK，因为它没接纳任何东西。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biz.trace import get_trace, list_traces
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import Command, CommandType
from core.utils.encoding import JsonObject

if TYPE_CHECKING:
    from bootstrap.runtime import Runtime


def answer(runtime: Runtime, command: Command, scope: Scope) -> tuple[str, JsonObject] | None:
    """执行一条只读 command。**不是查询命令就返回 `None`**，调用方改走接纳路径。

    返回的 event_type 一般与 command type 同名，客户端按 event_type 判别即可。
    """
    if command.type == CommandType.SESSION_STATUS:
        return CommandType.SESSION_STATUS, status(runtime, scope)
    if command.type == CommandType.TRACE_LIST:
        return CommandType.TRACE_LIST, list_traces(runtime.ctx.traces, scope)
    if command.type == CommandType.TRACE_GET:
        trace_id = str(command.payload["trace_id"])
        return CommandType.TRACE_GET, get_trace(runtime.ctx.traces, scope, trace_id)
    return None


def status(runtime: Runtime, scope: Scope) -> JsonObject:
    """当前 session 的可重连视图。全部读自已提交的 runtime 状态，不重新求值。"""
    state = runtime.ctx.store.read(scope)
    active = state["meta"].get("active_step")
    step = state["steps"].get(active, {})
    return {
        "run_id": state["meta"].get("run_id"),
        "step_id": active,
        "trace_id": step.get("trace_id"),
        "step_status": step.get("status", "idle"),
        "error": step.get("error") or state["meta"].get("intake_error"),
        "calls": list(state["calls"].values()),
        "messages": state["conversation"].get("messages", []),
        "compaction": {
            key: {field: value.get(field) for field in ("compression_id", "covers", "created_dt")}
            for key, value in state["compactions"].items()
        },
        "input_errors": [
            {"input_id": key, "error": entry["error"]}
            for key, entry in state["inputs"].items()
            if entry["phase"] == "rejected"
        ],
    }


__all__ = ["answer", "status"]
