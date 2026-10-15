"""传输层唯一拿到的对象。

`AgentApi` 本身不实现任何业务动作，它把一个 command 分派到对应的域，并且是唯一
知道「哪些 command 是只读的」的地方 —— 传输层靠 `answer` 返回 `None` 判断该回
ACK 还是该回 event，因此不必自己认识 query 这一类命令。

依赖方向单向 —— `biz` → `bootstrap`（拿装配好的 `Runtime`）→ `core`。
core 不反向 import 这里任何东西。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biz.session import open_session, status, submit
from biz.trace import get_trace, list_traces
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import Command, CommandType
from core.utils.encoding import JsonObject

if TYPE_CHECKING:
    from bootstrap.runtime import Runtime


class AgentApi:
    """runtime 的操作面与查询面。"""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    # -- 操作 ---------------------------------------------------------------

    def open_session(self, command: Command) -> Scope:
        return open_session(self._runtime, command)

    def submit(self, command: Command, scope: Scope) -> None:
        submit(self._runtime, command, scope)

    # -- 查询 ---------------------------------------------------------------

    def answer(self, command: Command, scope: Scope) -> tuple[str, JsonObject] | None:
        """只读命令的入口。**不是查询命令返回 `None`**，调用方改走 `submit`。

        返回的 event_type 与 command type 同名，客户端按 event_type 判别即可。
        """
        if command.type == CommandType.SESSION_STATUS:
            return CommandType.SESSION_STATUS, status(self._runtime, scope)
        if command.type == CommandType.TRACE_LIST:
            return CommandType.TRACE_LIST, list_traces(self._runtime.ctx.traces, scope)
        if command.type == CommandType.TRACE_GET:
            trace_id = str(command.payload["trace_id"])
            return CommandType.TRACE_GET, get_trace(self._runtime.ctx.traces, scope, trace_id)
        return None

    def status(self, scope: Scope) -> JsonObject:
        return status(self._runtime, scope)

    def traces(self, scope: Scope) -> JsonObject:
        return list_traces(self._runtime.ctx.traces, scope)

    def trace(self, scope: Scope, trace_id: str) -> JsonObject:
        return get_trace(self._runtime.ctx.traces, scope, trace_id)


__all__ = ["AgentApi"]
