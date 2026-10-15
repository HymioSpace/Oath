"""runtime 的操作面：会话绑定与 command 接纳。

`AgentApi` 是传输层唯一拿到的对象。命令的形状是 wire 的形状（`Command`），返回的是
可直接 JSON 序列化的对象 —— 这一层不做协议翻译，翻译是传输的事。

依赖方向单向 —— `biz` → `bootstrap`（拿装配好的 `Runtime`）→ `core`。
core 不反向 import 这里任何东西。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biz.query import answer, status
from biz.trace import get_trace, list_traces
from core.protocol.datatype.inbound import of_command
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import Command, CommandType
from core.protocol.error import WireProtocolError
from core.utils.encoding import JsonObject

if TYPE_CHECKING:
    from bootstrap.runtime import Runtime


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

    def answer(self, command: Command, scope: Scope) -> tuple[str, JsonObject] | None:
        """只读命令的入口。不是查询命令返回 `None`，调用方改走 `submit`。"""
        return answer(self._runtime, command, scope)

    def status(self, scope: Scope) -> JsonObject:
        return status(self._runtime, scope)

    def traces(self, scope: Scope) -> JsonObject:
        return list_traces(self._runtime.ctx.traces, scope)

    def trace(self, scope: Scope, trace_id: str) -> JsonObject:
        return get_trace(self._runtime.ctx.traces, scope, trace_id)


__all__ = ["AgentApi"]
