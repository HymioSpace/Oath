"""用户所有权与可选的有界委派目标；连接不改变身份。"""

import re
from dataclasses import dataclass
from typing import Any

from core.protocol.error import UserError

USER_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


def is_valid_user_id(value: str) -> bool:
    return isinstance(value, str) and re.fullmatch(USER_ID_PATTERN, value) is not None


@dataclass(frozen=True, slots=True, order=True)
class User:
    user_id: str
    agent_id: str = ""

    def __post_init__(self) -> None:
        if self.agent_id and not re.fullmatch(r"[a-f0-9]{32}", self.agent_id):
            raise UserError("Invalid agent_id")
        if not is_valid_user_id(self.user_id):
            raise UserError("user_id must match [A-Za-z0-9_-]{1,64}")

    @property
    def storage_id(self) -> str:
        return self.user_id + (":" + self.agent_id if self.agent_id else "")

    @classmethod
    def from_storage_id(cls, value: str):
        parts = value.split(":", 1)
        return cls(parts[0], parts[1] if len(parts) == 2 else "")

    def matches(self, record: dict[str, Any]) -> bool:
        return record.get("user_id") == self.storage_id

    def __str__(self) -> str:
        return self.storage_id
