"""面向客户端的操作与查询。

传输无关：WebSocket、HTTP、命令行都调这一层。命令的形状是 wire 的形状（`Command`），
返回的是可直接 JSON 序列化的对象 —— 这一层不做协议翻译，翻译是传输的事。

依赖方向：`biz` → `bootstrap`（拿装配好的 `Runtime`）→ `core`。core 不反向依赖这里。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.protocol.datatype.inbound import of_command
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.trace import Span
from core.protocol.datatype.wire import Command, CommandType
from core.protocol.error import WireProtocolError
from core.utils.encoding import JsonObject, JsonValue

if TYPE_CHECKING:
    from bootstrap.runtime import Runtime

_log = logging.getLogger("oath.biz")


class AgentApi:
    """runtime 的操作面与查询面。"""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    # -- 操作 ---------------------------------------------------------------

    def open_session(self, command: Command) -> Scope:
        """验 credential，绑定 session。认不出就抛错 —— 不回落到匿名账号。"""
        identity = self._runtime.authenticator.authenticate(
            str(command.payload.get("credential", ""))
        )
        return Scope(user_id=identity.user_id, session_id=identity.session_id)

    def submit(self, command: Command, scope: Scope) -> None:
        if command.session_id != scope.session_id:
            raise WireProtocolError("session 与认证身份不匹配", code="session_mismatch")
        if command.type == CommandType.PERMISSION_DECIDE:
            if command.payload.get("status") not in {"approved", "denied"}:
                raise WireProtocolError("审批只接受 approved / denied", code="bad_payload")
            if not isinstance(command.payload.get("invocation_id"), str):
                raise WireProtocolError("审批缺 invocation_id", code="bad_payload")
            ctx = self._runtime.ctx
            if ctx.tools.status(scope, str(command.payload["invocation_id"])) is None:
                raise WireProtocolError("找不到调用", code="not_found")
            command = Command(
                command.id,
                command.type,
                command.session_id,
                {**command.payload, "decided_by": scope.user_id},
            )
        if command.type == CommandType.USER_MESSAGE and not isinstance(
            command.payload.get("content"), str
        ):
            raise WireProtocolError("content 必须是文本", code="bad_payload")
        inbound = of_command(command)
        if inbound is not None:
            runtime = self._runtime
            runtime.buffers.accept(scope, inbound)
            ctx = runtime.ctx
            ctx.queue.put(scope, inbound)
            runtime.ensure_session(scope)

    # -- 查询 ---------------------------------------------------------------

    def status(self, scope: Scope) -> JsonObject:
        state = self._runtime.ctx.store.read(scope)
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
                key: {
                    field: value.get(field) for field in ("compression_id", "covers", "created_dt")
                }
                for key, value in state["compactions"].items()
            },
            "input_errors": [
                {"input_id": key, "error": entry["error"]}
                for key, entry in state["inputs"].items()
                if entry["phase"] == "rejected"
            ],
        }

    def traces(self, scope: Scope) -> JsonObject:
        """这个 scope 的 trace 列表，每条带 step 数与累计耗时。

        累计耗时在这里算而不是存下来：存了就是第二份真相，span 一改它就过期了。
        """
        traces = self._runtime.ctx.traces
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

    def trace(self, scope: Scope, trace_id: str) -> JsonObject:
        """一条 trace 的全部 span。找不到就是空列表 —— 与「无权限看」不可区分。"""
        spans = self._runtime.ctx.traces.spans(scope, trace_id)
        return {"trace_id": trace_id, "duration_ms": _elapsed(spans), "spans": _view(spans)}


def _view(spans: tuple[Span, ...]) -> list[JsonValue]:
    return [{**span.to_dict(), "duration_ms": span.duration_ms} for span in spans]


def _elapsed(spans: tuple[Span, ...]) -> float | None:
    """trace 的累计耗时：`max(end) - min(start)`。没有已结束的 span 就是 `None`。"""
    ends = [span.end_unix_nano for span in spans if span.end_unix_nano is not None]
    if not ends:
        return None
    return round((max(ends) - min(span.start_unix_nano for span in spans)) / 1_000_000, 3)


__all__ = ["AgentApi"]
