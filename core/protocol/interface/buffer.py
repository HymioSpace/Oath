"""`EventBuffer` 接口 —— consumer 侧的攒批缓冲。

**一个事实从队列搬进 buffer 那一刻起就不再会丢。** 这是整条链上唯一一处耐久性承诺：
队列是刻意可丢的（内存、重启即空），journal 是自洽的但要等 `Brain` 落盘，中间这一段
就是 buffer。

**`snapshot` 不删除。** 消费方在 `Brain.step` 返回「处理完了」之后才 `clear`。中间崩了，
重启后 `scopes()` 会把没 clear 的那批捞回来重跑一遍那一步，journal 的幂等键挡住重复
写入。

接口里只有 scope，没有路径 —— 换存储实现（SQLite）时契约不动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.scope import Scope


class EventBuffer(ABC):
    """三条持久化的攒批缓冲。"""

    @abstractmethod
    def put(self, scope: Scope, queue: str, items: Sequence[Inbound]) -> None:
        """追加一批。返回即代表这一批已经不会再丢。"""
        ...

    @abstractmethod
    def snapshot(self, scope: Scope, queue: str) -> tuple[Inbound, ...]:
        """读出这条队列现在的全部内容。**不删除**。"""
        ...

    @abstractmethod
    def clear(self, scope: Scope, queue: str) -> None:
        """这批处理完了，清掉。

        **只能在 `Brain.step` 返回 True 之后调。** 提前清等于承认这件事已经算数了 ——
        而它可能一条记录都还没落盘。
        """
        ...

    @abstractmethod
    def scopes(self) -> tuple[Scope, ...]:
        """还有没 `clear` 的内容的全部 scope。重启恢复的入口。"""
        ...


__all__ = ["EventBuffer"]
