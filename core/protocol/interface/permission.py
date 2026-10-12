"""权限系统的接口：应用侧实现 `PermissionPolicy`，投递方实现 `PermissionEventSink`。

分工与工具系统那边一致：

- `PermissionPolicy` 只回答「这个主体能不能调这个工具」。它不记录、不发事件、
  不知道有没有定时器 —— 那些一律 core 自己做。
- `PermissionEventSink` 是投递口。投递失败必须当场抛错。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype import PermissionEvent
from core.protocol.datatype.scope import Scope
from core.utils.encoding import JsonObject


class PermissionPolicy(ABC):
    """每次工具执行前的权限判定。应用侧实现（Hymio 的策略）。"""

    @abstractmethod
    def check(self, scope: Scope, tool: str, arguments: JsonObject) -> str:
        """返回 `PermissionVerdict` 的三值之一。

        **不抛异常表示拒绝。** 「拒绝」是 `PermissionVerdict.DENY` 这个返回值，
        不是异常 —— 因为「要问人」和「拒绝」在这个接口里都是正常结果。

        抛异常意味着**策略实现自己出错了**（读不到配置、连不上策略服务）。
        core 会 fail closed：当作拒绝处理，不放行。宁可误杀也不误放。
        """
        ...


class PermissionEventSink(ABC):
    """审批事件（请求与裁定）的投递口。

    同步调用，投递失败必须当场抛错 —— 静默吞掉会让一次待审批请求凭空消失，
    而调用方永远不会知道有这么一次调用。
    """

    @abstractmethod
    def emit(self, event: PermissionEvent) -> None: ...


__all__ = ["PermissionEventSink", "PermissionPolicy"]
