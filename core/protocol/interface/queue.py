"""三条输入队列：投递、取出、确认、等待新事件。

队列是中间件，不在这个接口里。ack、pending、消费组都是中间件自己的可用性保障 ——
用 Kafka 就是 offset 提交，用 redis streams 就是消费组。换中间件就是换一个实现，
`core/` 不动。

**队列不是可靠性的来源。** 一条输入被 consumer 取走、并且被逐条 `confirm` 之后，它住
在 consumer 的 buffer 里，那个 buffer 才要求持久。队列这边丢了一条，等价于这条输入还
没到达。journal 里只保存 Brain 认领过的事实。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.scope import Scope


class EventQueue(ABC):
    """三条按队列名分开的输入队列。"""

    @abstractmethod
    def add(self, scope: Scope, item: Inbound) -> None:
        """投递一件输入，进 `item.kind` 那条队列。

        不做去重、不做幂等：同一件事可以被投多次，消费方自己按 `source_id` 处理。
        """
        ...

    @abstractmethod
    def get_all(self, scope: Scope, queue: str) -> tuple[Inbound, ...]:
        """取出这条队列当前的全部待取输入。空队列返回空元组。

        **取出来不算收下。** 调用方把每一条持久接纳之后，要逐条 `confirm` 告诉队列这一条
        已经不归它管了。没确认的那些下次还会重投。
        """
        ...

    @abstractmethod
    def confirm(self, scope: Scope, queue: str, item: Inbound) -> None:
        """确认这一条已经被调用方持久接纳,队列可以放手了。

        逐条确认，不是整批。**一批里确认过的和没确认过的命运不同** —— 整批确认等于
        承认没落盘的那些也已经收下了。
        """
        ...

    @abstractmethod
    def listen(self, scope: Scope, queue: str, timeout: float) -> bool:
        """等到这条队列有新输入或超时，返回有没有等到。

        这是唯一的等待手段。返回 `False` 不代表出错，只是这段时间没有新东西 ——
        调用方本来就该周期性重新判定。
        """
        ...

    @abstractmethod
    def close(self, *, wait: bool = True) -> None:
        """停机。`listen` 之后立刻返回。"""
        ...


__all__ = ["EventQueue"]
