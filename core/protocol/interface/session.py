"""会话身份：credential 换来的东西。

连接建立时定下来，之后这条连接不再自称。**`user_id` 不来自帧里** —— 隔离键一旦能被
调用方伪造，越权读就不是漏洞而是默认行为。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    """credential 换来的身份。连接建立时定下来，之后这条连接不再自称。"""

    user_id: str
    session_id: str


class SessionAuthenticator(ABC):
    """把 credential 换成 `SessionIdentity`。

    刻意不返回 None 表示「认不出来」：那是异常，不是可选项。不回落到匿名账号。
    """

    @abstractmethod
    def authenticate(self, credential: str) -> SessionIdentity:
        """认不出就抛错 —— 不回落到匿名账号。"""
        ...


__all__ = ["SessionAuthenticator", "SessionIdentity"]
