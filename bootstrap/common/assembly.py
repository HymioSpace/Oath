"""执行依赖的装配。所有持久数据经 StoragePort，工具经服务契约。"""

from dataclasses import replace

from bootstrap.clients.context_provider import provider
from bootstrap.clients.tool_client import ToolClient
from bootstrap.common.extension_loader import load_extensions
from core.context import ContextManager
from core.journal.runtime_store import RuntimeStore
from core.protocol.datatype.user import User
from core.runtime import AgentContext
from core.skill import SkillSystem
from core.storage.adapters import (
    DurableEventSink,
    StoredBuffer,
    StoredJournal,
    StoredQueue,
    StoredTraces,
)


def build_context(config, storage, queue, tools, fence=None):
    extensions = load_extensions(
        config.agent.extension_dirs,
        kinds=frozenset({"skills"}),
        skill_dirs=config.agent.skill_dirs,
    )
    agent_config = config.agent
    if fence and User.from_storage_id(fence["user_id"]).agent_id:
        child_user = User.from_storage_id(fence["user_id"])
        child = storage.call(
            "subagent.get", user_id=child_user.user_id, agent_id=child_user.agent_id
        )
        agent_config = replace(
            agent_config,
            max_run_steps=child["max_steps"],
            max_tool_only_steps=child["max_steps"] + 1,
            max_run_seconds=max(1, child["deadline"] - child["created_at"]),
        )
    journal = StoredJournal(storage)
    return AgentContext(
        journal=journal,
        store=RuntimeStore(storage, fence=fence, patch_commits=True),
        queue=StoredQueue(queue, fence),
        buffer=StoredBuffer(storage, fence),
        sink=DurableEventSink(storage, fence),
        tools=ToolClient(tools, fence),
        context_manager=ContextManager(journal, agent_config),
        skills=SkillSystem(
            (config.agent.skill_dir, *extensions.skill_dirs), scan_dirs=config.agent.skill_scan_dirs
        ),
        traces=StoredTraces(storage),
        config=agent_config,
        context_provider=provider(agent_config),
    )
