"""User-scoped service operations; implementations may cross a network."""

from __future__ import annotations

from typing import Protocol

from core.protocol.datatype.user import User
from core.utils.encoding import JsonObject


class AgentUnavailable(Exception):
    """The agent process did not complete the request; never substitute empty data."""


class AgentAccess(Protocol):
    def compaction_events(self, *, since: str, limit: int = 5000) -> list[JsonObject]: ...

    def user_context(self, user: User) -> JsonObject: ...

    def conversation_history(self, user: User, *, before: int | None, limit: int) -> JsonObject: ...


__all__ = ["AgentAccess", "AgentUnavailable"]
