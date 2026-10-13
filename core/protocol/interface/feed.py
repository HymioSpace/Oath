"""`EventFeed` 接口 —— journal 上的游标视图。

一条按 type 过滤的内部队列。消费方拿到的永远是「当前位置之后属于这条 feed 的记录」，
推进位置要显式 `commit`：journal 是 pull 模型，消费方处理完才算数，中途崩了下次还能
重来。

位置只存内存是 v1 的取舍，重启后回到起点、事件重放一遍 —— fold 是纯函数，重放安全。
要持久化时它本身也是一条 journal 记录，不是另开一个存储。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype import Cursor, Record, Scope


class EventFeed(ABC):
    """一条按 type 过滤的、有位置的 journal 视图。"""

    @abstractmethod
    def pull(self, scope: Scope, feed: str, *, limit: int | None = None) -> tuple[Record, ...]:
        """从当前位置**之后**读属于这条 feed 的记录。

        返回的记录**不推进位置** —— 处理完要显式 `commit`，中途崩了下次还能重来。

        扫描途中**不属于这条 feed 的记录会被跳过并推进**，但只推到「最后一条被跳过
        的记录」为止：第一条命中的记录前面不能越过去。少了这个规则，一条
        `system.agent_step` 会把消费方永久卡在它前面。

        `limit` 限制的是**读到多少条日志行**，不是返回多少条。截断时不会有多余的
        `wake`，所以调用方要一直 `pull` 到返回空元组为止。

        返回空元组表示暂时没有新记录。
        """
        ...

    @abstractmethod
    def commit(self, scope: Scope, feed: str, cursor: Cursor) -> None:
        """推进位置。只能前进 —— 往回推会让已经处理过的记录重来一次。

        传一个不属于本实现的游标 kind，或往回推，抛 `CursorError`。
        """
        ...

    @abstractmethod
    def position(self, scope: Scope, feed: str) -> Cursor:
        """当前位置。还没消费过任何记录时是「第一条之前」（JSONL 是 `line:0`）。"""
        ...

    @abstractmethod
    def wake(self, scope: Scope) -> None:
        """通知：这个 scope 有新记录落进来了。

        由写入方在 `journal.append(...)` 返回成功后调用。journal 自己不通知 ——
        它是 pull 模型，不知道谁在消费。让 `EventFeed` 包一层 `append` 也没意义：
        幂等语义与指纹计算都在 journal 里，多一层只会多一处能写错的地方。
        """
        ...

    @abstractmethod
    def wait(self, scope: Scope, feed: str, *, timeout: float | None = None) -> bool:
        """等到这个 scope 上有新记录属于这条 feed。超时返回 False。

        返回 True 表示「有可读的了」，不保证 `pull` 一定非空 —— 醒来之后别的
        消费方可能已经先读走了。
        """
        ...

    @abstractmethod
    def reset(self, scope: Scope, feed: str | None = None) -> None:
        """把位置退回起点。`feed=None` 表示全部退回。

        恢复时的重放走这里：位置不持久化，重启后本来就是起点，这个方法是为了
        「往回退一点重读」这种刻意重放。
        """
        ...


__all__ = ["EventFeed"]
