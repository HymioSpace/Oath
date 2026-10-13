"""事件投递口。

**系统层面只有一个 sink。** 工具系统的终态事件与权限系统的审批事件都从这里出去 ——
它们本来就是同一件事的两头：`tool.result` 给 agent runtime 接着往下走，
`permission.requested` 给外部审批通道，`permission.resolved` 回到 agent。
分成两个投递口只会让实现方写两份几乎一样的代码，而下游拿到的事件流还是同一条。

投递口是 core 的边界，不是一个「业务能力注册表」：core 产生事件，实现方负责送，
但实现方**不**在这里插权限、改状态、调度回调 —— 那些一律 core 自己做。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype.event import AgentEvent


class AgentEventSink(ABC):
    """core 事件的唯一投递口。

    同步调用，投递失败必须当场抛错。静默吞掉会把结果丢在内存里，而调用方永远
    不会知道有这么一次调用 —— 工具系统把这种情况记成 `outcome_unknown`，权限
    系统直接把错误抛给发起派发的那一方。

    实现不需要是同步送达的最终出口（内部可以排队、可以重试），但**失败信号必须
    同步传回**：core 用它判断「对方到底知不知道这件事发生过」。
    """

    @abstractmethod
    def emit(self, event: AgentEvent) -> None: ...


__all__ = ["AgentEventSink"]
