"""三个持久化 buffer。跨 buffer 的认领与 step.input 在同一 store 事务中提交。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.scope import Scope


class EventBuffer(ABC):
    @abstractmethod
    def accept(self, scope: Scope, item: Inbound) -> bool:
        """可靠接纳，返回是否新增；同身份不同内容必须拒绝。"""
        ...

    @abstractmethod
    def receive(self, scope: Scope, item: Inbound) -> None:
        """consumer 确认进入本队列 buffer；保存首次 receipt，不重复计时。"""
        ...

    @abstractmethod
    def snapshot(self, scope: Scope, queue: str) -> tuple[Inbound, ...]:
        """仅返回该队列尚未认领的输入。"""
        ...

    @abstractmethod
    def unreceived(self, scope: Scope, queue: str) -> tuple[Inbound, ...]:
        """接纳已保存但尚未 buffer 的输入，用于恢复漏通知。"""
        ...

    @abstractmethod
    def scopes(self) -> tuple[Scope, ...]: ...
