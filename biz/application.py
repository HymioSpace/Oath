"""Business handlers. The trace read model is served from this process."""

from biz.trace import list_traces, query_trace
from core.protocol.datatype.cli import CliCommand
from core.protocol.datatype.user import User
from core.protocol.interface.agent_access import AgentAccess
from core.protocol.interface.identity import UserAuthenticator
from core.protocol.interface.trace import TraceStore
from core.utils.encoding import JsonObject, JsonValue


class BizApplication:
    def __init__(
        self,
        access: AgentAccess,
        authenticator: UserAuthenticator,
        commands: tuple[CliCommand, ...],
        traces: TraceStore,
    ) -> None:
        self.authenticator = authenticator
        self.commands = commands
        self._access = access
        #: 只读：trace 由 agent 写，这里只调 `trace_ids` / `spans`。
        self._traces = traces

    def list_commands(self) -> tuple[JsonObject, ...]:
        return tuple(
            {"name": c.name, "description": c.description, "parameters": c.parameters}
            for c in self.commands
        )

    def list_traces(self, user: User, *, limit: int, offset: int) -> JsonObject:
        return list_traces(self._traces, user, limit=limit, offset=offset)

    def query_trace(self, user: User, trace_id: str) -> JsonObject:
        return query_trace(self._traces, user, trace_id)

    def run_cli(self, user: User, name: str, arguments: JsonObject) -> JsonValue:
        command = next((c for c in self.commands if c.name == name), None)
        if command is None:
            raise KeyError(name)
        context = self._access.user_context(user)
        return command.handler(**arguments, user=user, user_context=context)
