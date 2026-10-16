"""输入的原始内容。

producer 只管投进队列，不落盘。consumer 收下之后它住在持久 buffer 里，Brain 认领时
才落 journal —— 队列和 buffer 都只是还没成为事实的过程。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.protocol.datatype.queue import QueueName
from core.protocol.datatype.wire import Command, CommandType
from core.protocol.error import WireProtocolError
from core.utils.encoding import JsonObject, utc_now_iso


@dataclass(frozen=True, slots=True)
class Inbound:
    """一件事的原始内容。

    `kind` 就是队列名（`QueueName` 三个值之一），不用再翻译一遍。

    `source` 是产生它的 command 或事件的 type（`user.message` / `tool.terminal` /
    `run.cancel`）。消费方按它分辨同一条队列里的不同来路 —— `system` 里既有取消也有
    Supervisor 的定时唤醒，两者的处理方式不一样。

    `occur_dt` 是事情发生的时刻。工具结果那一路的等待时限从**第一个结果到达**算起，
    不是从派发算起：慢的是工具，不是 agent 决定等它。
    """

    kind: str
    source: str
    payload: JsonObject = field(default_factory=dict)
    #: command 的 `id` / 工具调用的 `invocation_id`。幂等键的原料。
    source_id: str = ""
    occur_dt: str = ""

    def to_dict(self) -> JsonObject:
        return {
            "kind": self.kind,
            "source": self.source,
            "payload": dict(self.payload),
            "source_id": self.source_id,
            "occur_dt": self.occur_dt,
        }

    @classmethod
    def from_dict(cls, obj: JsonObject) -> Inbound:
        payload = obj.get("payload")
        return cls(
            kind=str(obj.get("kind", "")),
            source=str(obj.get("source", "")),
            payload=dict(payload) if isinstance(payload, dict) else {},
            source_id=str(obj.get("source_id", "")),
            occur_dt=str(obj.get("occur_dt", "")),
        )


def of_command(command: Command) -> Inbound | None:
    """用户输入与控制命令分别进入 user / system；查询与心跳不产生输入。"""
    if command.type in CommandType.QUERY:
        return None
    if command.type == CommandType.USER_MESSAGE:
        # `user_context` 照原样带下去：覆盖式的维护由 consumer 做，这里不裁剪也不解释。
        context = command.payload.get("user_context")
        return Inbound(
            kind=QueueName.USER,
            source=command.type,
            payload={
                "content": str(command.payload.get("content", "")),
                "user_context": dict(context) if isinstance(context, dict) else {},
            },
            source_id=command.id,
            occur_dt=utc_now_iso(),
        )
    if command.type in {
        CommandType.RUN_CANCEL,
        CommandType.PERMISSION_DECIDE,
        CommandType.STEP_RETRY,
    }:
        return Inbound(
            kind=QueueName.SYSTEM,
            source=command.type,
            payload=dict(command.payload),
            source_id=command.id,
            occur_dt=utc_now_iso(),
        )
    raise WireProtocolError(f"{command.type} 还没有接进来", code="unsupported_command")


__all__ = ["Inbound", "of_command"]
