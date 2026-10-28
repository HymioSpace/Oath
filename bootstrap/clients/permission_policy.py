"""Application-owned permission policy evaluated before tool admission."""

from __future__ import annotations

import httpx

from core.protocol.datatype import PermissionDecision, PermissionRequest
from core.protocol.error import PermissionDeniedError
from core.protocol.interface.permission import PermissionPolicy


class RemotePermissionPolicy(PermissionPolicy):
    def __init__(self, url: str, token: str, *, timeout: float = 5.0) -> None:
        self.url = url
        self.headers = {"Authorization": "Bearer " + token}
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    def check(self, request: PermissionRequest) -> PermissionDecision:
        try:
            response = self.client.post(
                self.url,
                headers=self.headers,
                json={
                    "user_id": request.user.user_id,
                    "agent_id": request.user.agent_id,
                    "invocation_id": request.invocation_id,
                    "batch_id": request.batch_id,
                    "run_id": request.run_id,
                    "channel": request.channel,
                    "tool": request.tool,
                    "arguments": request.arguments,
                },
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Policy response must be an object")
            approval = data.get("approval")
            if approval is not None and not isinstance(approval, dict):
                raise ValueError("Policy approval must be an object")
            return PermissionDecision(
                verdict=str(data["verdict"]),
                reason=str(data.get("reason", ""))[:500],
                approval=approval,
                grant_key=str(data.get("grant_key", ""))[:256],
                policy_version=str(data.get("policy_version", ""))[:64],
            )
        except Exception as exc:
            raise PermissionDeniedError("Permission policy unavailable") from exc

    def close(self) -> None:
        self.client.close()
