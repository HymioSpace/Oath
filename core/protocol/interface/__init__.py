"""core 的接口，按模块分文件。"""

from __future__ import annotations

from core.protocol.interface.agent_access import AgentAccess, AgentUnavailable
from core.protocol.interface.brain import WakeRequests
from core.protocol.interface.buffer import EventBuffer
from core.protocol.interface.compaction import Compactor
from core.protocol.interface.detail import DetailStore
from core.protocol.interface.event import AgentEventSink
from core.protocol.interface.execution import ExecutionEnvironment, ExecutionResult
from core.protocol.interface.identity import UserAuthenticator, UserIdentity
from core.protocol.interface.journal import Journal
from core.protocol.interface.llm import DeltaChannel, DeltaSink, ModelProvider
from core.protocol.interface.permission import PermissionPolicy
from core.protocol.interface.queue import EventQueue
from core.protocol.interface.tool import Tool, ToolSystem
from core.protocol.interface.trace import TraceStore
from core.protocol.interface.transport import OutboundTransport, SendResult

__all__ = [
    "AgentAccess",
    "AgentEventSink",
    "AgentUnavailable",
    "WakeRequests",
    "Compactor",
    "DeltaChannel",
    "DeltaSink",
    "DetailStore",
    "EventBuffer",
    "EventQueue",
    "ExecutionEnvironment",
    "ExecutionResult",
    "Journal",
    "ModelProvider",
    "OutboundTransport",
    "PermissionPolicy",
    "SendResult",
    "UserAuthenticator",
    "UserIdentity",
    "Tool",
    "ToolSystem",
    "TraceStore",
]
