"""待处理输入的集合：consumer 收下之后、brain 认领之前，住在 buffer 里。

buffer 是可靠性的来源。队列那边丢一条只等于「这条输入还没到」；从 buffer 出去的东西
必须在重启后还在。journal 只保存 brain 认领过的事实。

**一个进程一个 scope，所以键里没有 scope。** 三条队列共用一个实例，靠队列名分开。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype.inbound import Inbound


class EventBuffer(ABC):
    """按队列名分开的待处理输入。"""

    @abstractmethod
    def add(self, queue: str, item: Inbound) -> None:
        """追加一条。顺序即到达顺序，重复不合并。"""
        ...

    @abstractmethod
    def get_all(self, queue: str) -> tuple[Inbound, ...]:
        """取走这条队列当前的全部，顺序不变。

        **取走即移出。** 要读但不拿走的东西不该从这里取 —— 调用方需要的东西还在的时候，
        它就还在。
        """
        ...

    @abstractmethod
    def close(self) -> None: ...


__all__ = ["EventBuffer"]
