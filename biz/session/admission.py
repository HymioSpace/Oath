"""一条 command 写盘之前的检查与定稿。

校验全部在写入之前做完：一条格式不对的命令不该先进 buffer 再被发现不对。
只读命令在这里直接返回 `None` —— 它们不产生输入，也就没有 payload 可校验。
"""

from __future__ import annotations

from core.protocol.datatype.inbound import Inbound, of_command
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import Command, CommandType
from core.protocol.error import WireProtocolError
from core.runtime import AgentContext


def admit(ctx: AgentContext, command: Command, scope: Scope) -> Inbound | None:
    """校验一条要写入的命令，返回对应的输入。**只读命令返回 `None`。**"""
    if command.session_id != scope.session_id:
        raise WireProtocolError("session 与认证身份不匹配", code="session_mismatch")
    if command.type == CommandType.PERMISSION_DECIDE:
        command = _decide(ctx, command, scope)
    if command.type == CommandType.USER_MESSAGE and not isinstance(
        command.payload.get("content"), str
    ):
        raise WireProtocolError("content 必须是文本", code="bad_payload")
    return of_command(command)


def _decide(ctx: AgentContext, command: Command, scope: Scope) -> Command:
    """审批裁定定稿。`decided_by` 由认证身份覆盖 —— 客户端自称的不算数。"""
    if command.payload.get("status") not in {"approved", "denied"}:
        raise WireProtocolError("审批只接受 approved / denied", code="bad_payload")
    invocation_id = command.payload.get("invocation_id")
    if not isinstance(invocation_id, str):
        raise WireProtocolError("审批缺 invocation_id", code="bad_payload")
    if ctx.tools.status(scope, invocation_id) is None:
        raise WireProtocolError("找不到调用", code="not_found")
    return Command(
        command.id,
        command.type,
        command.session_id,
        {**command.payload, "decided_by": scope.user_id},
    )


__all__ = ["admit"]
