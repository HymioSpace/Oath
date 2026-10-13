"""`Brain` —— 决定「跑什么」的那个东西。

context build → LLM → tool call → compress。`Intake` 从队列取到记录交给它，它决定
下一步：调模型、追加消息，还是什么都不做。

这一轮**不实现**：context 怎么 build、LLM 怎么调、什么时候压缩、并发怎么控，全是策略。
接口只表达一件事：**给我一条输入，我把该做的做完。**

`step` 返回之后这次处理才算完 —— 它可能有副作用（模型已经调过、工具已经派发），崩了
之后哪些能重放是策略，记账不归它管。
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

        **要处理完全部副作用才返回。** 返回之后 `Intake` 才会推进队列游标；返回之后
        再崩，重放会重复调模型或重复派发工具。

        「跑什么」全在这里：追加消息、决定要不要再调一次模型、派发哪些工具、什么时候
        压缩上下文。`EventRouter` 只判断这条要不要给它，不判断该怎么跑。
        """
        ...

    @abstractmethod
    def close(self, *, wait: bool = True) -> None:
        """停机。`wait=False` 不等正在跑的 `step` 返回。"""
        ...


__all__ = ["Brain"]
