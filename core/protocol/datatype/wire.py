"""WS v2：用户身份绑定到连接；事件以 event_seq 确认和补发。"""

from dataclasses import dataclass
from typing import Any

from core.protocol.error import WireProtocolError


class WireVersion:
    CURRENT = 2


class CommandType:
    AGENT_ATTACH = "agent.attach"
    USER_MESSAGE = "user.message"
    PERMISSION_DECIDE = "permission.decide"
    RUN_CANCEL = "run.cancel"
    STEP_RETRY = "step.retry"
    AGENT_STATUS = "agent.status"
    EVENT_ACK = "event.ack"
    PING = "ping"
    QUERY = frozenset({AGENT_STATUS, EVENT_ACK, PING})
    ALL = frozenset({AGENT_ATTACH, USER_MESSAGE, PERMISSION_DECIDE, RUN_CANCEL, STEP_RETRY, *QUERY})


@dataclass(frozen=True)
class Command:
    id: str
    type: str
    payload: dict[str, Any]

    @classmethod
    def parse(cls, raw):
        if not isinstance(raw, dict) or raw.get("v") != 2:
            raise WireProtocolError("Expected WS v2", code="bad_version")
        if set(raw) - {"v", "id", "type", "payload"}:
            raise WireProtocolError("Unknown command fields", code="bad_frame")
        if not isinstance(raw.get("id"), str) or not 1 <= len(raw["id"]) <= 128:
            raise WireProtocolError("Invalid command id", code="bad_frame")
        if raw.get("type") not in CommandType.ALL or not isinstance(raw.get("payload", {}), dict):
            raise WireProtocolError("Invalid command", code="bad_frame")
        return cls(raw["id"], raw["type"], raw.get("payload", {}))

    def to_json(self):
        return {"v": 2, "id": self.id, "type": self.type, "payload": self.payload}


def ack(command_id, **extra):
    return {"v": 2, "type": "ack", "id": command_id, **extra}


def pong(command_id):
    return {"v": 2, "type": "pong", "id": command_id}


def error_frame(command_id, code, message):
    return {"v": 2, "type": "error", "id": command_id, "code": code, "message": message}
