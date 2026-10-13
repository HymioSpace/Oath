"""`QueueRunner` —— 消费三条队列的那个循环。

它和 `Brain` 不是一回事，理由是**两者的失败语义不同**：

- `QueueRunner` 没有副作用。取记录、分发、推进游标，崩了下次重来就行。
- `Brain` 可能已经调过模型、派发过工具。崩了之后哪些能重放、哪些不能，是策略。

所以游标由 `QueueRunner` 推进而不是 `Brain`：`Brain` 崩在半路的记录不该被算成已处理，
但也不该由 `Brain` 自己负责记账 —— 记账规则和 LLM 无关。

分工一句话：**`QueueRunner` 决定「现在该不该跑」，`Brain` 决定「跑什么」。**

输入全从 `EventFeed` 来，不从内存传消息：进程重启后队列位置回到起点、事件重放，
而 fold 是纯函数，重放安全。从内存传消息则没有这个性质 —— 崩了就少一段，而且没人
说得清少了什么。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype.scope import Scope


class QueueRunner(ABC):
    """三条队列的消费循环。"""

    @abstractmethod
    def poll(self, scope: Scope) -> bool:
        """把这个 scope 三条队列里可读的记录取出来、分发完。

        返回 True 表示至少分发了一条。**返回 False 不代表出错**：队列空就是队列空。
        处理完才算数，位置由本方法推进 —— 分发途中崩了，下次会重来。
        """
        ...

    @abstractmethod
    def wait(self, scope: Scope, *, timeout: float | None = None) -> bool:
        """等到有可读记录再 `poll`。超时返回 False。

        空转轮询 journal 是浪费 IO，也是把「有没有事做」与「谁负责等」搞混。
        """
        ...

    @abstractmethod
    def close(self, *, wait: bool = True) -> None:
        """停机。`wait=False` 不等正在跑的 `poll` 返回。

        停机不写任何「收尾」记录：没处理的输入在 journal 里，下次起来还在。
        """
        ...


__all__ = ["QueueRunner"]
