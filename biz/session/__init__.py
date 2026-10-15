"""会话的写入面与状态投影。

`open_session` 绑定身份，`submit` 接纳输入，`status` 给出可重连的视图。三件事都
只对着已提交的 runtime 状态，不重新求值 —— 重连看到的就是当前真实的样子。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biz.session.admission import admit
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import Command
from core.utils.encoding import JsonObject

if TYPE_CHECKING:
    from bootstrap.runtime import Runtime


def open_session(runtime: Runtime, command: Command) -> Scope:
    """验 credential，绑定 session。认不出就抛错 —— 不回落到匿名账号。"""
    identity = runtime.authenticator.authenticate(str(command.payload.get("credential", "")))
    return Scope(user_id=identity.user_id, session_id=identity.session_id)


def submit(runtime: Runtime, command: Command, scope: Scope) -> None:
    """校验并可靠接纳一条命令。校验不过抛错，绝不写半条。"""
    inbound = admit(runtime.ctx, command, scope)
    if inbound is None:
        return
    runtime.buffers.accept(scope, inbound)
    runtime.ctx.queue.put(scope, inbound)
    runtime.ensure_session(scope)


def status(runtime: Runtime, scope: Scope) -> JsonObject:
    """当前 session 的可重连视图。全部读自已提交的 runtime 状态。"""
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


__all__ = ["open_session", "status", "submit"]
