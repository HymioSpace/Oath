"""WS transport depends on a single-scope agent."""

from typing import Protocol

from core.outbound import ClientChannel
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import Command
from core.protocol.interface.session import SessionAuthenticator
from core.utils.encoding import JsonObject


class SessionService(Protocol):
    authenticator: SessionAuthenticator

    @property
    def connections(self) -> int:
        """在线连接数。manager 按它回收 scope。"""

    def open_session(self, command: Command) -> Scope: ...
    def submit(self, command: Command, scope: Scope) -> None: ...
    def status(self, scope: Scope) -> JsonObject: ...
    def user_context(self, scope: Scope) -> JsonObject: ...
    def attach(self, scope: Scope, channel: ClientChannel) -> None: ...
    def detach(self, scope: Scope, channel: ClientChannel) -> None: ...


__all__ = ["SessionService"]
