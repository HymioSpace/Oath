"""外部提供的身份；认证方式由接入层装配。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UserIdentity:
    user_id: str
    expires_at: float | None = None
    agent_id: str = ""


class UserAuthenticator(ABC):
    @abstractmethod
    def authenticate(self, credential: str, user_id: str | None = None) -> UserIdentity:
        """失败抛错；受信接入可使用调用方提供的 user_id。"""
        ...


__all__ = ["UserAuthenticator", "UserIdentity"]
