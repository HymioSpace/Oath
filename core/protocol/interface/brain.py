"""`Brain` —— 决定「跑什么」的那个东西。

context build → LLM → tool call → compress。`QueueRunner` 拿到记录交给它，它决定
下一步；`QueueRunner` 不管这一步是调模型、追加消息还是什么都不做。

这一轮**不实现**：context 怎么 build、LLM 怎么调、什么时候压缩、并发怎么控，全是
策略。接口只表达一件事：**给我一条输入，我把该做的做完。**

`Brain` 可能已经有副作用（模型已经调过、工具已经派发），所以它崩了之后哪些能重放是
策略问题，不是记账问题。记账归 `QueueRunner`。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype.journal import Record
from core.protocol.datatype.scope import Scope


class Brain(ABC):
    """agent 行为的决策方。"""

    @abstractmethod
    def step(self, scope: Scope, record: Record) -> None:
        """处理一条队列记录，把它带来的下一步做完。

        **要处理完全部副作用才返回。** 返回之后再崩，记录已经 commit，重放会重复
        调模型或重复派发工具 —— 这正是 `QueueRunner` 与 `Brain` 分开的理由。

        「跑什么」全在这里：追加消息、决定要不要再调一次模型、派发哪些工具、什么时候
        压缩上下文。`QueueRunner` 不看这些。
        """
        ...

    @abstractmethod
    def close(self, *, wait: bool = True) -> None:
        """停机。`wait=False` 不等正在跑的 `step` 返回。"""
        ...


__all__ = ["Brain"]
