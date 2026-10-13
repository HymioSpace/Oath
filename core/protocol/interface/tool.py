"""工具系统的接口：扩展作者实现 `Tool`，调用方用 `ToolSystem`。

分工写死在接口形状里：

- `Tool` 只提供**业务动作**和**内容**。没有权限钩子、没有超时属性、没有状态
  迁移、没有投递 —— 这些一律 core 自己做，不给扩展留「权限扩展」「调度扩展」
  这类位置。
- `ToolSystem` 掌握生命周期：定义汇总、批量派发、状态查询。
- 终态事件从这里出去，但投递口不归这一层 —— 系统只有一个
  `AgentEventSink`（见 `core/protocol/interface/event.py`），权限的审批事件走
  同一个口。投递失败由实现抛错，core 据此判定结果未知。
- 权限不在这里。判定是三值的（allow / deny / ask），带审批流与有效期，
  是独立的一套机制，见 `core/protocol/interface/permission.py`。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from core.protocol.datatype import (
    BatchExecution,
    Scope,
    ToolCallRequest,
    ToolDefinition,
    ToolExecution,
    ToolInvocation,
    ToolResult,
)


class Tool(ABC):
    """一个业务工具。

    实现只需提供定义和一个 `execute`。超时、权限、并发、事件投递都在 core 那一侧。
    """

    @property
    @abstractmethod
    def definition(self) -> ToolDefinition:
        """给模型的定义。注册时校验一次，形状不合法会在装配期暴露。"""
        ...

    @abstractmethod
    def execute(self, invocation: ToolInvocation) -> ToolResult:
        """跑这个工具。

        正常返回即成功。业务失败抛 `ToolExecutionError`，其他异常一律记成
        `FAILED` + `kind=unexpected`。core 不会因为这里抛错而中断整批。

        线程里跑的，**没有 deadline 强制收手**：`invocation.deadline` 只是提示，
        拿它给自己要发的 I/O 设更短的内部超时即可；core 到点就不再等待。
        """
        ...


class ToolSystem(ABC):
    """工具系统的调用面。"""

    @abstractmethod
    def definitions(self) -> tuple[ToolDefinition, ...]:
        """汇总当前可用的工具定义，交给 context。

        顺序稳定（按名字排序）是硬要求：同一份工具集在不同进程里必须序列化成
        逐字节相同的请求体，否则缓存、幂等指纹和 trace 都对不上。
        """
        ...

    @abstractmethod
    def execute(self, scope: Scope, requests: Sequence[ToolCallRequest]) -> BatchExecution:
        """派发一批调用，**立即返回**。

        - 只收批次，没有单条路径：单个调用是长度为 1 的批次。同一批里的调用
          互不依赖，没有理由串行。
        - 返回时没有一个工具已经跑完。返回值只回答「谁被接下了」「谁当场被拒了」。
        - 逐项独立：一个项被拒不影响同批其他项。
        - 整批入参不合法（空批次等）才抛 `ToolRequestError`，且不产生任何执行。

        结果不走返回值，走终态事件。
        """
        ...

    @abstractmethod
    def status(self, scope: Scope, invocation_id: str) -> ToolExecution | None:
        """查一次执行的状态。查不到返回 `None` —— 这是查询，不是命令。

        查询键是 `invocation_id`（Oath 的调用 id），**不是**模型给的
        `tool_call_id`：后者只是透传回对话历史的配对值，不构成一个可查询的标识。

        **跨 scope 一律查不到**：用别的 session 去问一个执行 id 不会得到「它
        存在」，也不会泄露任何东西。
        """
        ...

    @abstractmethod
    def shutdown(self, *, wait: bool = True) -> None:
        """停止接收新执行，并按 `wait` 决定是否等执行中的调用收尾。

        执行中的调用**不会被终止** —— 没有安全的线程终止手段。进程退出后它们的
        结果由 journal 对账，不是这里的事。
        """
        ...


__all__ = ["Tool", "ToolSystem"]
