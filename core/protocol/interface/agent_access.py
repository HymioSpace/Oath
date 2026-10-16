"""Scope-scoped service operations; implementations may cross a network."""

from __future__ import annotations

from typing import Protocol

from core.protocol.datatype.scope import Scope
from core.utils.encoding import JsonObject


class AgentUnavailable(Exception):
    """The agent process did not complete the request; never substitute empty data."""


class AgentAccess(Protocol):
    def user_context(self, scope: Scope) -> JsonObject: ...


__all__ = ["AgentAccess", "AgentUnavailable"]
