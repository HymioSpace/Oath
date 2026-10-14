"""可靠接纳的输入通知。producer 先保存 source_id 与 payload，再通知内存队列。

三个 consumer 分别持久化 receipt；Brain 原子认领，不决定输入是否应当落盘。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from core.protocol.datatype.wire import Command, CommandType
from core.protocol.error import WireProtocolError
from core.utils.encoding import JsonObject, utc_now_iso


class InboundKind:
    """入站事件的三种 kind。每一种决定它进哪条队列，也决定由谁等它。"""

    #: 用户说了什么。有它就醒。
    USER: Final = "user"
    #: 工具真实终态，ToolConsumer 按批次可见结果、呈现状态和 deadline 判断唤醒。
    TOOL_RESULT: Final = "tool_result"
    #: 控制面指令与 Supervisor 的定时唤醒。
    INVOKE: Final = "invoke"

    ALL: Final = frozenset({USER, TOOL_RESULT, INVOKE})


@dataclass(frozen=True, slots=True)
class Inbound:
    """一件事的原始内容。

    `source` 是产生它的 command 或事件的 type（`user.message` / `tool.terminal` /
    `run.cancel`）。消费方按它分辨同一 kind 里的不同来路 —— `invoke` 里既有取消也有
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


@dataclass(frozen=True, slots=True)
class Batch:
    """一次交给 `Brain` 的全部待处理输入，按队列分好。

    **不是「刚到的那些」，是三条 buffer 现在的全部内容。** 「这一轮齐了没有」是
    跨批次的判断 —— 上一轮剩下的结果还在 buffer 里，不在 `tool` 那一组新输入里。
    """

    user: tuple[Inbound, ...] = ()
    tool: tuple[Inbound, ...] = ()
    system: tuple[Inbound, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.user or self.tool or self.system)


def of_command(command: Command) -> Inbound | None:
    """用户输入与控制命令分别进入 user / system；查询与心跳不产生输入。"""
    if command.type in {CommandType.PING, CommandType.SESSION_STATUS}:
        return None
    if command.type == CommandType.USER_MESSAGE:
        return Inbound(
            kind=InboundKind.USER,
            source=command.type,
            payload={"content": str(command.payload.get("content", ""))},
            source_id=command.id,
            occur_dt=utc_now_iso(),
        )
    if command.type in {
        CommandType.RUN_CANCEL,
        CommandType.PERMISSION_DECIDE,
        CommandType.STEP_RETRY,
    }:
        return Inbound(
            kind=InboundKind.INVOKE,
            source=command.type,
            payload=dict(command.payload),
            source_id=command.id,
            occur_dt=utc_now_iso(),
        )
    raise WireProtocolError(f"{command.type} 还没有接进来", code="unsupported_command")


__all__ = ["Batch", "Inbound", "InboundKind", "of_command"]
