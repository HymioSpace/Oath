"""scope 凭证。

两层：服务级凭证预置在配置里，能调 `create_session`；session 级凭证由 manager 签发，
只认那一个 scope。**session 凭证不进配置文件，也不进命令行** —— 走环境变量，
否则 `ps` 里任何人都能拿到别人的 session。
"""

import os

from core.protocol.error import PermissionDeniedError
from core.protocol.interface.session import SessionAuthenticator, SessionIdentity

#: manager 往子进程注入 session 凭证用的环境变量名。
SESSION_CREDENTIAL_ENV = "OATH_SESSION_CREDENTIAL"


def read_session_credential() -> str:
    """子进程读自己的 session 凭证。没有就是没有，不回落到配置里的东西。"""
    return os.environ.get(SESSION_CREDENTIAL_ENV, "")


class TokenAuthenticator(SessionAuthenticator):
    def __init__(self, credentials: dict[str, tuple[str, str]]) -> None:
        self._credentials = credentials

    def authenticate(self, credential: str) -> SessionIdentity:
        identity = self._credentials.get(credential)
        if identity is None:
            raise PermissionDeniedError("Unknown credential")
        return SessionIdentity(user_id=identity[0], session_id=identity[1])


__all__ = [
    "SESSION_CREDENTIAL_ENV",
    "TokenAuthenticator",
    "read_session_credential",
]
