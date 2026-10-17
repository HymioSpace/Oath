"""执行依赖的装配。所有持久数据经 StoragePort，工具经服务契约。"""

from bootstrap.clients.tool_client import ToolClient
from bootstrap.common.extension_loader import load_extensions
from core.context import ContextManager
from core.journal.runtime_store import RuntimeStore
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
    journal = StoredJournal(storage)
    return AgentContext(
        journal=journal,
        store=RuntimeStore(storage, fence=fence),
        queue=StoredQueue(queue, fence),
        buffer=StoredBuffer(storage, fence),
        sink=DurableEventSink(storage, fence),
        tools=ToolClient(tools, fence),
        context_manager=ContextManager(journal, config.agent),
        skills=SkillSystem(
            (config.agent.skill_dir, *extensions.skill_dirs), scan_dirs=config.agent.skill_scan_dirs
        ),
        traces=StoredTraces(storage),
        config=config.agent,
    )
