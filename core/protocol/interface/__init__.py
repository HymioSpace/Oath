"""core 的接口，按模块分文件。"""

from __future__ import annotations

from core.protocol.interface.brain import Brain
from core.protocol.interface.event import AgentEventSink
from core.protocol.interface.feed import EventFeed
from core.protocol.interface.intake import (
    EventRouter,
    Intake,
    RoutingDecision,
    RoutingOutcome,
    SessionAuthenticator,
    SessionIdentity,
)
from core.protocol.interface.journal import Journal
from core.protocol.interface.permission import PermissionPolicy
from core.protocol.interface.tool import Tool, ToolSystem
from core.protocol.interface.transport import OutboundTransport, SendResult

__all__ = [
    "AgentEventSink",
    "Brain",
    "EventFeed",
    "EventRouter",
    "Intake",
    "Journal",
    "OutboundTransport",
    "PermissionPolicy",
    "RoutingDecision",
    "RoutingOutcome",
    "SendResult",
    "SessionAuthenticator",
    "SessionIdentity",
    "Tool",
    "ToolSystem",
]
