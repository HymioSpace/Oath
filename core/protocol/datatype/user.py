"""唯一的执行隔离身份；连接与任务不改变用户身份。"""

import re
from dataclasses import dataclass

from core.protocol.error import UserError

USER_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


def is_valid_user_id(value: str) -> bool:
    return isinstance(value, str) and re.fullmatch(USER_ID_PATTERN, value) is not None


@dataclass(frozen=True, slots=True, order=True)
class User:
    user_id: str

    def __post_init__(self) -> None:
        if not is_valid_user_id(self.user_id):
            raise UserError("user_id must match [A-Za-z0-9_-]{1,64}")

    def matches(self, record: dict) -> bool:
        return record.get("user_id") == self.user_id

    def __str__(self) -> str:
        return self.user_id
