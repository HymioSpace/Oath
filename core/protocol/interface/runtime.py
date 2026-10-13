"""`Runtime` —— 跑 agent 的那个循环。

不是 Agent 这个名字的理由：`AgentState` / `agent step` / `agent runtime` 已经被占用了，
再叫 Agent 会分不清谁是什么。`Runtime` 就是「agent 行为的执行体」。

这一轮**不实现** context build、LLM 调用、tool call 派发、压缩 —— 那些是策略。
接口只表达一件事：**从三条队列里取出输入，跑完一轮，把结果落回 journal**。

输入全从 `EventFeed` 来，不从内存传消息：进程重启后队列位置回到起点、事件重放，
而 fold 是纯函数，重放安全。从内存传消息则没有这个性质 —— 崩了就少一段，
而且没人说得清少了什么。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype.scope import Scope


class Runtime(ABC):
    """agent 行为的执行体。"""

    @abstractmethod
    def step(self, scope: Scope) -> bool:
        """把这个 scope 当前所有可消费的输入处理一轮。

        返回 True 表示至少处理掉了一条输入；False 表示三条队列都空 —— 阻塞等待
        的循环据此决定是收工还是睡下。**返回 False 不代表出错**：可以理解的
        状态就是状态字段，不是异常。
        """
        ...

    @abstractmethod
    def close(self, *, wait: bool = True) -> None:
        """停机。`wait=False` 不等正在跑的 `step` 返回。

        停机不写任何「收尾」记录：没处理的输入在 journal 里，下次起来还在。
        """
        ...


__all__ = ["Runtime"]
