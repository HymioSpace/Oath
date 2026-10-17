"""权限系统的接口：应用侧实现 `PermissionPolicy`。

- `PermissionPolicy` 只回答「这个主体能不能调这个工具」。它不记录、不发事件、
  不知道有没有定时器 —— 那些一律 core 自己做。
- 审批事件的投递口**不在这一层**：系统只有一个 `AgentEventSink`
  （见 `core/protocol/interface/event.py`），工具终态与审批事件走同一个口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype.user import User
from core.utils.encoding import JsonObject


class PermissionPolicy(ABC):
    """每次工具执行前的权限判定。应用侧实现（Hymio 的策略）。"""

    @abstractmethod
    def check(self, user: User, tool: str, arguments: JsonObject) -> str:
        """返回 `PermissionVerdict` 的三值之一。

        **不抛异常表示拒绝。** 「拒绝」是 `PermissionVerdict.DENY` 这个返回值，
        不是异常 —— 因为「要问人」和「拒绝」在这个接口里都是正常结果。

        抛异常意味着**策略实现自己出错了**（读不到配置、连不上策略服务）。
        core 会 fail closed：当作拒绝处理，不放行。宁可误杀也不误放。
        """
        ...


__all__ = ["PermissionPolicy"]
