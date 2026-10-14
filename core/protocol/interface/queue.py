"""三条内存通知队列，分别订阅和消费。

producer 必须先可靠接纳输入。queue 丢失可从 journal 重建；取出通知不删除持久化输入。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.scope import Scope


class EventQueue(ABC):
    """三条按 kind 分开的输入队列。"""

    @abstractmethod
    def put(self, scope: Scope, item: Inbound) -> None:
        """入队。队列由 `item.kind` 决定。

        返回只代表通知已排队；可靠接纳由调用方在 put 之前完成。
        """
        ...

    @abstractmethod
    def drain(self, scope: Scope, queue: str, *, limit: int = 64) -> tuple[Inbound, ...]:
        """从这条队列一次取光，最多 `limit` 条。空队列立刻返回空元组。

        **不分页。** 一轮里同时到的 20 条工具结果分 20 次取，就是 20 次可能被插队的
        机会。`limit` 只在防止无界批次时起作用，不是页大小。
        """
        ...

    @abstractmethod
    def pending(self, scope: Scope, queue: str) -> int:
        """这条队列现在有多少条待取。"""
        ...

    @abstractmethod
    def revision(self, scope: Scope, queue: str) -> int:
        """消费前读 revision，随后 wait 不会漏掉在判定期间到来的通知。"""
        ...

    @abstractmethod
    def notify(self, scope: Scope) -> None:
        """只通知重新判定，不加入消息。"""
        ...

    @abstractmethod
    def wait(
        self,
        scope: Scope,
        *,
        queue: str | None = None,
        timeout: float | None = None,
        after: int | None = None,
    ) -> bool: ...

    @abstractmethod
    def close(self, *, wait: bool = True) -> None:
        """停机。`wait` 之后立刻返回，不再等待。

        通知随队列消失；可靠接纳但尚未处理的输入保留在 journal。
        """
        ...


__all__ = ["EventQueue"]
