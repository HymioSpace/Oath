"""`AgentContext` —— core 里共享基础设施的收口。

**进程一份，不是每个 session 一份。** journal、队列、投递口、工具系统这些都按 scope
自己分流，跨会话共用同一个对象；`scope` 仍然当参数传，没有收进这里。

**只装共享基础设施。** `provider`、`buffer`、`authenticator` 是各组件自己的协作对象，
不进这里 —— 全放进来就成了 service locator，测试要靠改共享状态而不是传替身。

它是一次装配完成的普通值，不是能边填边用的半成品：造 `sink` 要先有 `queue`，造 `tools`
要先有 `sink`，顺序由装配层决定，所以这里没有任何字段允许「先空着后补上」。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.context import ContextManager
from core.protocol.datatype.config import AgentConfig
from core.protocol.interface import AgentEventSink, EventQueue, Journal, ToolSystem


@dataclass(frozen=True, slots=True)
class AgentContext:
    """core 各组件共用的那一批对象。

    持有它们不是为了省参数 —— 是为了让「谁在写 journal、谁在发事件」只有一个答案。
    """

    #: 唯一的真相。**只由 brain 写。**
    journal: Journal
    #: 三条内存输入队列。只管叫醒，不碰 journal。
    queue: EventQueue
    #: 事件的唯一投递口。工具的终态事件从这里出去 —— 直推客户端，入队等 brain 落盘。
    sink: AgentEventSink
    tools: ToolSystem
    #: 把 journal 投影成一次模型请求。
    context_manager: ContextManager
    config: AgentConfig


__all__ = ["AgentContext"]
