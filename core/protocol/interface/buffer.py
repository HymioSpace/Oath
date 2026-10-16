"""待处理输入的集合：consumer 收下之后、brain 认领之前，住在 buffer 里。

buffer 是可靠性的来源。队列那边丢一条只等于「这条输入还没到」；从 buffer 出去的东西
必须在重启后还在。journal 只保存 brain 认领过的事实。

**每次转移的规矩是：先在下一处持久接纳，再从这一处删掉。** 认领一条输入分两步 ——
`peek` 读出来、journal 事务提交、再 `remove` 掉那一条。崩在两步之间的任何位置，输入都
还在：没提交就还在 buffer 里等下一轮，提交了没删就靠 journal 去重认掉。取走即删除的
写法没有这个性质 —— 那是先删后写，崩在中间就两头都没有。

**键里带 scope。** 一个 data_root 下有多个 agent 进程，键只有队列名的话，一个 scope 的
认领会取走另一个 scope 的输入。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.scope import Scope


class EventBuffer(ABC):
    """按 `(scope, 队列名)` 分开的待处理输入。"""

    @abstractmethod
    def add(self, scope: Scope, queue: str, item: Inbound) -> None:
        """追加一条。顺序即到达顺序，重复不合并。"""
        ...

    @abstractmethod
    def peek(self, scope: Scope, queue: str) -> tuple[Inbound, ...]:
        """读出这条队列当前的全部，**不移出**。

        读出来是为了拿到它去别处落下，落下成功之后才 `remove`。读不动的是 `remove`
        能解决的场景 —— 那个场景就是这里存在的理由。
        """
        ...

    @abstractmethod
    def remove(self, scope: Scope, queue: str, item: Inbound) -> None:
        """移出指定的那一条。

        按 `(kind, source_id)` 认人，不是按位置。认不在的直接返回：这条可能已经被别的
        认领路径删掉了。
        """
        ...

    @abstractmethod
    def close(self) -> None: ...


__all__ = ["EventBuffer"]
