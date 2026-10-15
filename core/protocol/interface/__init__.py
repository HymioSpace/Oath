"""core 的接口，按模块分文件。"""

from __future__ import annotations

from core.protocol.interface.brain import Brain
from core.protocol.interface.buffer import EventBuffer
from core.protocol.interface.compaction import Compactor
from core.protocol.interface.detail import DetailStore
from core.protocol.interface.event import AgentEventSink
from core.protocol.interface.journal import Journal
from core.protocol.interface.llm import DeltaChannel, DeltaSink, ModelProvider
from core.protocol.interface.permission import PermissionPolicy
from core.protocol.interface.queue import EventQueue
from core.protocol.interface.session import SessionAuthenticator, SessionIdentity
from core.protocol.interface.tool import Tool, ToolSystem
from core.protocol.interface.trace import TraceStore
from core.protocol.interface.transport import OutboundTransport, SendResult

__all__ = [
    "AgentEventSink",
    "Brain",
    "Compactor",
    "DeltaChannel",
    "DeltaSink",
    "DetailStore",
    "EventBuffer",
    "EventQueue",
    "Journal",
    "ModelProvider",
    "OutboundTransport",
    "PermissionPolicy",
    "SendResult",
    "SessionAuthenticator",
    "SessionIdentity",
    "Tool",
    "ToolSystem",
    "TraceStore",
]
