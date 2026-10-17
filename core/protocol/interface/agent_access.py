"""User-scoped service operations; implementations may cross a network."""

from __future__ import annotations

from typing import Protocol

from core.protocol.datatype.user import User
from core.utils.encoding import JsonObject


class AgentUnavailable(Exception):
    """The agent process did not complete the request; never substitute empty data."""


class AgentAccess(Protocol):
    def user_context(self, user: User) -> JsonObject: ...


__all__ = ["AgentAccess", "AgentUnavailable"]
