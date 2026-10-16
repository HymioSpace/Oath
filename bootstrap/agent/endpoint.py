"""Scope-authenticated service requests on the agent WS listener."""

from __future__ import annotations

import asyncio
import json
import logging

from websockets.asyncio.server import ServerConnection

from core.protocol.datatype.scope import Scope
from core.protocol.error import PermissionDeniedError
from core.protocol.interface.agent_access import AgentAccess
from core.protocol.interface.session import SessionAuthenticator
from core.utils.encoding import JsonObject, canonical_json

_log = logging.getLogger(__name__)


class AgentEndpoint:
    def __init__(
        self, agent: AgentAccess, authenticator: SessionAuthenticator, *, timeout: float
    ) -> None:
        self._agent, self._auth, self._timeout = agent, authenticator, timeout

    def dispatch(self, request: object) -> JsonObject:
        if not isinstance(request, dict) or request.get("v") != 1:
            raise ValueError("Invalid request")
        if set(request) != {"v", "id", "method", "credential", "params"}:
            raise ValueError("Invalid envelope")
        if not isinstance(request["id"], str) or not 0 < len(request["id"]) <= 128:
            raise ValueError("Invalid id")
        if not isinstance(request["credential"], str):
            raise ValueError("Invalid credential")
        identity = self._auth.authenticate(request["credential"])
        scope = Scope(identity.user_id, identity.session_id)
        params = request["params"]
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        if request["method"] == "context.get" and not params:
            return self._agent.user_context(scope)
        raise ValueError("Unknown method or invalid parameters")

    async def serve(self, connection: ServerConnection) -> None:
        request_id = ""
        try:
            raw = await asyncio.wait_for(connection.recv(), timeout=self._timeout)
            request = json.loads(raw)
            if isinstance(request, dict) and isinstance(request.get("id"), str):
                request_id = request["id"][:128]
            result = await asyncio.wait_for(
                asyncio.to_thread(self.dispatch, request), self._timeout
            )
            reply: JsonObject = {"id": request_id, "result": result}
        except PermissionDeniedError:
            reply = {"id": request_id, "error": {"code": "unauthorized"}}
        except (ValueError, TypeError):
            reply = {"id": request_id, "error": {"code": "bad_request"}}
        except Exception:
            # Do not log requests: they contain credentials and personal context.
            reply = {"id": request_id, "error": {"code": "unavailable"}}
        try:
            await asyncio.wait_for(connection.send(canonical_json(reply)), self._timeout)
        finally:
            await connection.close()


__all__ = ["AgentEndpoint"]
