"""`EventQueue` 接口 —— 三条待路由的输入队列。

队列装的是**已经落盘、还没有处理**的记录。它是内存里的排队结构，不是 journal 的视图：
取出即出队，没有位置要推进，也没有「处理完才 commit」这回事。崩溃时队列清空，里面未处理
的记录还留在 journal 里 —— 真相在 journal，不在队列。

这与 journal 兜底当队列的做法差在两处：入队出队是内存操作，不读日志；重启后队列是空的，
journal 里那些没处理过的记录要靠上层决定怎么办。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype.journal import Record
from core.protocol.datatype.scope import Scope


class EventQueue(ABC):
    """三条按类型分开的输入队列。"""

    @abstractmethod
    def put(self, scope: Scope, record: Record) -> None:
        """入队。队列由 `record.type` 决定；不是输入类型的记录不排队。

        调用方要先 append 进 journal —— 队列存的是已落盘的记录，先落盘再排队，
        崩溃时队列里不会有没写进日志的东西。
        """
        ...

    @abstractmethod
    def get(self, scope: Scope, queue: str, *, timeout: float | None = None) -> Record | None:
        """从这条队列取一条。空队列就等到有为止，超时返回 None。

        `timeout=0` 不等，立即返回 —— 队列空就是队列空，不是错。
        """
        ...

    @abstractmethod
    def pending(self, scope: Scope, queue: str) -> int:
        """这条队列现在有多少条待处理。"""
        ...

    @abstractmethod
    def wait(self, scope: Scope, *, timeout: float | None = None) -> bool:
        """等到这个 scope 上任意一条队列非空。超时返回 False。

        返回 True 不保证 `get` 一定拿得到 —— 醒来之后别的消费方可能先取走了。
        """
        ...

    @abstractmethod
    def close(self, *, wait: bool = True) -> None:
        """停机。`get` 与 `wait` 之后立刻返回，不再等待。"""
        ...


__all__ = ["EventQueue"]
