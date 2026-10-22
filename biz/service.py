"""Business API contract; handlers execute in the biz service."""

from typing import Protocol

from core.protocol.datatype.user import User
from core.protocol.interface.identity import UserAuthenticator
from core.utils.encoding import JsonObject, JsonValue


class BizService(Protocol):
    authenticator: UserAuthenticator

    def list_commands(self) -> tuple[JsonObject, ...]: ...
    def list_traces(self, user: User, *, limit: int, offset: int) -> JsonObject: ...
    def query_trace(self, user: User, trace_id: str, *, summary: bool = False) -> JsonObject: ...
    def run_cli(self, user: User, name: str, arguments: JsonObject) -> JsonValue: ...
