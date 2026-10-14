"""`Brain` —— 决定「跑什么」的那个东西。

`SessionConsumer` 攒好一批 `Inbound` 交给它，它决定这批算不算一次输入：该落盘就落盘、
该调模型就调模型、还没到点就说还没到点。

**落盘在 `Brain`，不在队列、不在 producer。** 「我发起了一个 toolcall」和「我收到了它的
结果」只有 `Brain` 看得全 —— 工具系统知道结果，但不知道这一轮是什么。

## 契约

`step` 返回 `True` 表示这批处理完了，消费方可以 `clear` buffer；返回 `False` 表示**现在
还不该处理**，buffer 里的东西原样留着。这一支是为工具结果准备的：一轮里五个调用回来两个，
剩三个还在跑，journal 里那三个槽位还空着，这时候跑一次模型等于拿半个答案去猜。

`wakeup_delay` 回答的是「那还要等多久」—— 没到点时消费方按它给的秒数等，而不是空转
轮询或者干等新输入：工具一直不回来的时候不会有新 `put`，没人叫醒它。

`step` 返回之前所有副作用都要落完。返回之后再崩，重放会重复调模型 —— 幂等键挡住重复
写入，挡不住重复调用外部服务，所以顺序不能反。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype.inbound import Batch
from core.protocol.datatype.scope import Scope


class Brain(ABC):
    """agent 行为的决策方，也是 journal 里对话记录唯一的写入方。"""

    @abstractmethod
    def step(self, scope: Scope, batch: Batch) -> bool:
        """处理一批输入，把它带来的下一步做完。

        返回 `False` 表示现在还不该处理这批（典型是这一轮工具结果还没齐），调用方
        据此不清 buffer。抛错表示处理失败 —— buffer 同样不清，下一轮还会拿到它。
        """
        ...

    @abstractmethod
    def wakeup_delay(self, scope: Scope, batch: Batch) -> float | None:
        """这一批还没处理的话，还要等多少秒才会被处理。

        `None` 表示纯等新输入进来 —— 正在等的不是时间。
        """
        ...

    @abstractmethod
    def close(self, *, wait: bool = True) -> None:
        """停机。`wait=False` 不等正在跑的 `step` 返回。"""
        ...


__all__ = ["Brain"]
