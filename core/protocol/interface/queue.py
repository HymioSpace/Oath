"""`EventQueue` 接口 —— 三条排队的输入队列。

队列装的是**还没进对话历史、还没落盘**的 `Inbound`。它是内存里的排队结构，只回答
「有东西来了」：取出即出队，没有位置要推进，也没有「处理完才 commit」这回事。

**队列是刻意可丢的。** 它上面还有一个持久化的 `EventBuffer` —— 一个事实从队列搬进
buffer 那一刻起就不再会丢。这与 journal 兜底当队列的做法差在两处：入队出队不读不写
日志；重启后队列是空的，没搬走的那一批直接丢。

跟 journal 也不沾关系。producer（WebSocket、工具系统）不碰 journal —— 「我发起了一个
toolcall」和「我收到了它的结果」都不是它们的视角，只有 `Brain` 知道这一轮是什么。
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

        返回即代表排上了 —— **不代表落盘，也不代表 agent 会回应**。两者都还没发生。
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
    def wait(self, scope: Scope, *, timeout: float | None = None) -> bool:
        """等到这个 scope 上任意一条队列非空。超时返回 False。

        返回 True 不保证 `drain` 一定拿得到东西 —— 醒来之后东西可能已经被搬进 buffer。
        """
        ...

    @abstractmethod
    def close(self, *, wait: bool = True) -> None:
        """停机。`wait` 之后立刻返回，不再等待。

        队列里剩下的随队列一起消失。它们既没落盘也没持久化 —— 见文件头。
        """
        ...


__all__ = ["EventQueue"]
