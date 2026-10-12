"""Scope：`(user_id, session_id)` 隔离键。

所有资源都按它定位 —— journal、事件运行时、工具系统、biz 查询共用。

校验在任何路径拼接之前完成。这两个值来自认证主体或授权委托，不接受调用方
任意伪造，但未校验就拼进路径等于把目录穿越的口子开在文件系统上。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from core.protocol.error import ScopeError
from core.utils.encoding import JsonObject

#: `user_id` / `session_id` 的合法字符集。
SCOPE_ID_PATTERN: Final = r"^[A-Za-z0-9_-]{1,64}$"

_SCOPE_ID_RE: Final = re.compile(SCOPE_ID_PATTERN)


def is_valid_scope_id(value: str) -> bool:
    """判断 `user_id` / `session_id` 是否符合字符集要求。"""
    return _SCOPE_ID_RE.fullmatch(value) is not None


def _check(value: str, field: str) -> None:
    if not isinstance(value, str) or not is_valid_scope_id(value):
        raise ScopeError(f"{field} 不合法: {value!r}，要求匹配 [A-Za-z0-9_-]{{1,64}}")


@dataclass(frozen=True, slots=True, order=True)
class Scope:
    """第一层隔离（用户）+ 第二层隔离与串行化边界（会话）。"""

    user_id: str
    session_id: str

    def __post_init__(self) -> None:
        _check(self.user_id, "user_id")
        _check(self.session_id, "session_id")

    @classmethod
    def of(cls, user_id: str, session_id: str) -> Scope:
        return cls(user_id=user_id, session_id=session_id)

    def matches(self, record: JsonObject) -> bool:
        """记录里的 scope 是否与本 scope 一致。

        记录里冗余存了 `user_id` / `session_id`，即使文件已经按 scope 分目录。
        这道冗余防线挡住的是：文件被手工搬过、路径拼接出错、并发下写错文件。
        不一致就该报错，而不是当成另一个 session 的记录读进来。
        """
        return record.get("user_id") == self.user_id and record.get("session_id") == self.session_id

    def __str__(self) -> str:
        return f"{self.user_id}/{self.session_id}"
