"""工具扩展提供定义与业务动作；ToolSystem 管批次、审批、执行资格和唯一终态。

工具真实终态和可靠输入在同一事务提交，再通知 tool queue 与客户端。
在线投递失败不改变执行结果。权限策略只返回 allow / deny / ask。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from core.protocol.datatype import (
    BatchExecution,
    ToolCallRequest,
    ToolDefinition,
    ToolExecution,
    ToolInvocation,
    ToolResult,
    User,
)


class Tool(ABC):
    """一个业务工具。

    实现只需提供定义和一个 `execute`。超时、权限、并发、事件投递都在 core 那一侧。
    """

    allow_subagent: bool = True
    #: Expose only inside an independent delegated context.
    subagent_only: bool = False
    #: `None` exposes the tool on every channel. A set limits provider exposure and
    #: execution to those client channels.
    expose_channels: frozenset[str] | None = None

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
    def submit_batch(
        self, user: User, batch_id: str, requests: Sequence[ToolCallRequest]
    ) -> BatchExecution:
        """登记完整成员后异步接纳；初次集合只冻结一次。"""
        ...

    @abstractmethod
    def batch_status(self, user: User, batch_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def apply_permission(
        self,
        user: User,
        invocation_id: str,
        decision: str,
        command_id: str,
        *,
        scope: str = "once",
        client_id: str = "",
        channel: str = "",
        reason: str = "",
        action_digest: str = "",
    ) -> bool:
        """原子裁定并提交原调用；裁定和执行意图不能分开提交。"""
        ...

    @abstractmethod
    def cancel(self, user: User, run_id: str | None = None) -> None: ...

    @abstractmethod
    def definitions(self, *, channel: str | None = None) -> tuple[ToolDefinition, ...]:
        """汇总当前可用的工具定义，交给 context。

        顺序稳定（按名字排序）是硬要求：同一份工具集在不同进程里必须序列化成
        逐字节相同的请求体，否则缓存、幂等指纹和 trace 都对不上。
        """
        ...

    @abstractmethod
    def execute(self, user: User, requests: Sequence[ToolCallRequest]) -> BatchExecution:
        """派发一批调用，**立即返回**。

        - 只收批次，没有单条路径：单个调用是长度为 1 的批次。同一批里的调用
          互不依赖，没有理由串行。
        - 返回值表示批次已登记或已存在；接纳异步进行，结果与状态通过事件和查询读取。
        - 逐项独立：一个项被拒不影响同批其他项。
        - 整批入参不合法（空批次等）才抛 `ToolRequestError`，且不产生任何执行。

        结果不走返回值，走终态事件。
        """
        ...

    @abstractmethod
    def status(self, user: User, invocation_id: str) -> ToolExecution | None:
        """查一次执行的状态。查不到返回 `None` —— 这是查询，不是命令。

        查询键是 `invocation_id`（Oath 的调用 id），**不是**模型给的
        `tool_call_id`：后者只是透传回对话历史的配对值，不构成一个可查询的标识。

        **跨 user 一律查不到**：用别的 session 去问一个执行 id 不会得到「它
        存在」，也不会泄露任何东西。
        """
        ...

    @abstractmethod
    def shutdown(self, *, wait: bool = True) -> None:
        """停止接收和派发，并按 `wait` 决定是否等待维护线程退出。

        执行中的调用**不会被终止** —— 没有安全的线程终止手段。进程退出后它们的
        结果由 journal 对账，不是这里的事。
        """
        ...


__all__ = ["Tool", "ToolSystem"]
