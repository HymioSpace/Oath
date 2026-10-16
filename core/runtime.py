"""Core 的共享依赖；store 是原子提交入口，三个 consumer 各自负责唤醒。"""

from __future__ import annotations

from dataclasses import dataclass

from core.context import ContextManager
from core.journal.runtime_store import RuntimeStore
from core.protocol.datatype.config import AgentConfig
from core.protocol.interface import (
    AgentEventSink,
    EventBuffer,
    EventQueue,
    Journal,
    ToolSystem,
    TraceStore,
)
from core.skill.system import SkillSystem


@dataclass(frozen=True, slots=True)
class AgentContext:
    """core 各组件共用的那一批对象。

    持有它们不是为了省参数 —— 是为了让「谁在写 journal、谁在发事件」只有一个答案。
    """

    #: 各模块共用的追加日志。
    journal: Journal
    store: RuntimeStore
    #: 三条输入队列。中间件，不是可靠性的来源。
    queue: EventQueue
    #: 待认领输入。consumer 收下就进这里，brain 从这里认领并落 journal。
    buffer: EventBuffer
    #: 客户端在线投递口；推送失败不影响已保存的结果，重连查询状态。
    sink: AgentEventSink
    tools: ToolSystem
    #: 把 journal 投影成一次模型请求。
    context_manager: ContextManager
    #: 启动时扫好的技能索引，只读。
    skills: SkillSystem
    #: 可观测记录。派生数据：写失败只丢 span，不影响推理。
    traces: TraceStore
    config: AgentConfig


__all__ = ["AgentContext"]
