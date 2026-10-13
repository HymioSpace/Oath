"""core 的接口，按模块分文件。"""

from __future__ import annotations

from core.protocol.interface.event import AgentEventSink
from core.protocol.interface.journal import Journal
from core.protocol.interface.permission import PermissionPolicy
from core.protocol.interface.tool import Tool, ToolSystem

__all__ = [
    "AgentEventSink",
    "Journal",
    "PermissionPolicy",
    "Tool",
    "ToolSystem",
]
