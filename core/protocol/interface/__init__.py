"""core 的接口，按模块分文件。"""

from __future__ import annotations

from core.protocol.interface.event import AgentEventSink
from core.protocol.interface.feed import EventFeed
from core.protocol.interface.ingress import (
    EventRouter,
    Ingress,
    RoutingDecision,
    RoutingOutcome,
    SessionAuthenticator,
    SessionIdentity,
)
from core.protocol.interface.journal import Journal
from core.protocol.interface.permission import PermissionPolicy
from core.protocol.interface.runtime import Runtime
from core.protocol.interface.tool import Tool, ToolSystem
from core.protocol.interface.transport import OutboundTransport, SendResult

__all__ = [
    "AgentEventSink",
    "EventFeed",
    "EventRouter",
    "Ingress",
    "Journal",
    "OutboundTransport",
    "PermissionPolicy",
    "RoutingDecision",
    "RoutingOutcome",
    "Runtime",
    "SendResult",
    "SessionAuthenticator",
    "SessionIdentity",
    "Tool",
    "ToolSystem",
]
