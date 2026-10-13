"""拼一个 `AgentContext` 给测试用。

真 journal、真的 `ContextManager`；队列、sink、工具系统都有静默的默认值 —— 各测试
只把自己真正要断言的那一两个换成替身，其余的一致即可。
"""

from __future__ import annotations

from collections.abc import Sequence

from core.context import ContextManager
from core.protocol.datatype.config import AgentConfig, ModelProviderConfig
from core.protocol.datatype.event import AgentEvent
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.tool import (
    BatchExecution,
    ToolCallRequest,
    ToolDefinition,
    ToolExecution,
)
from core.protocol.interface.event import AgentEventSink
from core.protocol.interface.journal import Journal
from core.protocol.interface.queue import EventQueue
from core.protocol.interface.tool import ToolSystem
from core.queue import InMemoryEventQueue
from core.runtime import AgentContext

#: 模型与 key 都是占位 —— 测的是接线，没有 provider 会真的去调它。
DEFAULT_CONFIG = AgentConfig(
    system_prompt="你是助手",
    provider=ModelProviderConfig(model="m1", api_base="http://x", api_key_env="TEST_KEY"),
)


class SilentSink(AgentEventSink):
    """什么也不做。填那些不关心投递的测试。"""

    def emit(self, event: AgentEvent) -> None:
        return None


class NoTools(ToolSystem):
    """没有工具，也不会被派发。填那些不关心工具的测试。"""

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return ()

    def execute(self, scope: Scope, requests: Sequence[ToolCallRequest]) -> BatchExecution:
        return BatchExecution()

    def status(self, scope: Scope, invocation_id: str) -> ToolExecution | None:
        return None

    def shutdown(self, *, wait: bool = True) -> None:
        return None


def agent_ctx(
    journal: Journal,
    *,
    sink: AgentEventSink | None = None,
    tools: ToolSystem | None = None,
    queue: EventQueue | None = None,
    config: AgentConfig | None = None,
) -> AgentContext:
    config = config or DEFAULT_CONFIG
    return AgentContext(
        journal=journal,
        queue=queue or InMemoryEventQueue(),
        sink=sink or SilentSink(),
        tools=tools or NoTools(),
        context_manager=ContextManager(journal, config),
        config=config,
    )


__all__ = ["DEFAULT_CONFIG", "NoTools", "SilentSink", "agent_ctx"]
