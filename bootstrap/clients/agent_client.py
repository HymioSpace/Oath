"""Network implementation of AgentAccess. Requests never attach a chat session."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable

from websockets.sync.client import connect

from core.protocol.datatype.scope import Scope
from core.protocol.error import PermissionDeniedError, WireProtocolError
from core.protocol.interface.agent_access import AgentUnavailable
from core.utils.encoding import JsonObject, canonical_json


class AgentClient:
    """按 scope 找它那个 agent 进程。地址与凭证都由 manager 给，不配在这里。"""

    def __init__(
        self,
        url_of: Callable[[Scope], str],
        credential_of: Callable[[Scope], str],
        *,
        timeout: float = 5.0,
    ) -> None:
        self._url_of = url_of
        self._credential_of = credential_of
        self._timeout = timeout

    def user_context(self, scope: Scope) -> JsonObject:
        return self._call(scope, "context.get", {})

    def _call(
        self, scope: Scope, method: str, params: JsonObject, *, timeout: float | None = None
    ) -> JsonObject:
        credential = self._credential_of(scope)
        if not credential:
            raise PermissionDeniedError("Scope has no credential")
        budget = timeout if timeout is not None else self._timeout
        deadline = time.monotonic() + budget
        request_id = uuid.uuid4().hex
        try:
            with connect(
                self._url_of(scope).rstrip("/") + "/service",
                open_timeout=budget,
                close_timeout=min(budget, 0.2),
                max_size=16 << 20,
            ) as connection:
                connection.send(
                    canonical_json(
                        {
                            "v": 1,
                            "id": request_id,
                            "method": method,
                            "credential": credential,
                            "params": params,
                        }
                    )
                )
                raw = connection.recv(timeout=max(0.001, deadline - time.monotonic()))
                reply = json.loads(raw)
        except Exception as exc:
            raise AgentUnavailable("Agent service request failed") from exc
        if not isinstance(reply, dict) or reply.get("id") != request_id:
            raise AgentUnavailable("Invalid agent response")
        error = reply.get("error")
        if error:
            code = error.get("code") if isinstance(error, dict) else "internal_error"
            if code == "unauthorized":
                raise PermissionDeniedError("Agent rejected credential")
            if code == "bad_request":
                raise WireProtocolError("Invalid service request", code="bad_payload")
            raise AgentUnavailable("Agent could not complete request")
        value = reply.get("result")
        if not isinstance(value, dict):
            raise AgentUnavailable("Invalid agent result")
        return value


__all__ = ["AgentClient"]
