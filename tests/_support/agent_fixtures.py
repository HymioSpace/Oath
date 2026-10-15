"""拼一个 `AgentContext` 给测试用。

真 journal、真的 `ContextManager`；队列、sink、工具系统都有静默的默认值 —— 各测试
只把自己真正要断言的那一两个换成替身，其余的一致即可。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from core.context import ContextManager
from core.journal.runtime_store import RuntimeStore
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
from core.protocol.interface.trace import TraceStore
from core.queue import InMemoryEventQueue
from core.runtime import AgentContext
from core.skill import SkillSystem
from tests._support.trace_fixtures import RecordingTraceStore

#: 模型与 key 都是占位 —— 测的是接线，没有 provider 会真的去调它。技能目录指向一个
#: 不存在的位置：索引必须为空，测试不能读开发者本机的技能目录。
DEFAULT_CONFIG = AgentConfig(
    system_prompt="你是助手",
    provider=ModelProviderConfig(model="m1", api_base="http://x", api_key_env="TEST_KEY"),
    skill_dir=Path("/nonexistent/skills"),
)


class SilentSink(AgentEventSink):
    """什么也不做。填那些不关心投递的测试。"""

    def emit(self, event: AgentEvent) -> None:
        return None


class NoTools(ToolSystem):
    """没有工具，也不会被派发。填那些不关心工具的测试。"""

    def submit_batch(
        self, scope: Scope, batch_id: str, requests: Sequence[ToolCallRequest]
    ) -> BatchExecution:
        return BatchExecution()

    def batch_status(self, scope: Scope, batch_id: str) -> dict[str, Any] | None:
        return None

    def apply_permission(
        self, scope: Scope, invocation_id: str, decision: str, command_id: str
    ) -> bool:
        return False

    def cancel(self, scope: Scope, run_id: str | None = None) -> None:
        pass

    def recover_scope(self, scope: Scope) -> None:
        pass

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
    skills: SkillSystem | None = None,
    traces: TraceStore | None = None,
) -> AgentContext:
    config = config or DEFAULT_CONFIG
    return AgentContext(
        journal=journal,
        store=RuntimeStore(journal),
        queue=queue or InMemoryEventQueue(),
        sink=sink or SilentSink(),
        tools=tools or NoTools(),
        context_manager=ContextManager(journal, config),
        skills=skills or SkillSystem(config.skill_dir),
        traces=traces or RecordingTraceStore(),
        config=config,
    )


__all__ = ["DEFAULT_CONFIG", "NoTools", "SilentSink", "agent_ctx"]
