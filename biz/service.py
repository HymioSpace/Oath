"""Business API contract; handlers execute in the biz service."""

from typing import Protocol

from core.protocol.datatype.scope import Scope
from core.protocol.interface.session import SessionAuthenticator
from core.utils.encoding import JsonObject, JsonValue


class BizService(Protocol):
    authenticator: SessionAuthenticator

    def list_commands(self) -> tuple[JsonObject, ...]: ...
    def list_traces(self, scope: Scope, *, limit: int, offset: int) -> JsonObject: ...
    def query_trace(self, scope: Scope, trace_id: str) -> JsonObject: ...
    def run_cli(self, scope: Scope, name: str, arguments: JsonObject) -> JsonValue: ...
