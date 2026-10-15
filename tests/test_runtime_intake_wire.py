"""真实装配与 WebSocket command handler；模型和 socket 使用受控替身。"""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from websockets.asyncio.server import ServerConnection

from biz.api import AgentApi
from bootstrap.runtime import Runtime
from bootstrap.ws import _serve_connection
from core.protocol.datatype.config import OathConfig
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import Command
from core.protocol.error import WireProtocolError
from core.tool.impl import ThreadedToolSystem
from tests._support.intake_fixtures import ScriptedProvider, until
from tests._support.tool_fixtures import EchoTool, VerdictPolicy
from tests.test_agent_brain import call_response

#: 服务端发出的一帧。
Frame = dict[str, Any]


class Connection:
    """一条连接替身：只实现 `_serve_connection` 用到的 send / close / 异步迭代。

    帧队列里放的是 dict，`close()` 放一个 `None` 当结束哨兵。
    """

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[Frame | None] = asyncio.Queue()
        self.frames: list[Frame] = []
        self.on_send: Callable[[Frame], None] = lambda frame: None

    def __aiter__(self) -> "Connection":
        return self

    async def __anext__(self) -> str:
        value = await self.incoming.get()
        if value is None:
            raise StopAsyncIteration
        return json.dumps(value)

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        self.on_send(frame)
        self.frames.append(frame)

    async def close(self) -> None:
        await self.incoming.put(None)

    async def command(self, type_: str, id_: str, payload: Frame | None = None) -> None:
        frame: Frame = {"v": 1, "type": type_, "id": id_, "payload": payload or {}}
        if type_ != "session.open":
            frame["session_id"] = "s1"
        await self.incoming.put(frame)

    async def wait_for(self, predicate: Callable[[Frame], bool]) -> Frame | None:
        return await asyncio.to_thread(
            until, lambda: next((f for f in self.frames if predicate(f)), None)
        )

    def serve(self, runtime: Runtime) -> asyncio.Task[None]:
        """按 `_serve_connection` 声明的参数类型起这条连接的处理协程。"""
        return asyncio.create_task(
            _serve_connection(runtime, AgentApi(runtime), cast(ServerConnection, self))
        )


def tool_system(runtime: Runtime) -> ThreadedToolSystem:
    """装配层放进去的一定是 `ThreadedToolSystem`，`AgentContext.tools` 上只声明了 `ToolSystem`。"""
    return cast(ThreadedToolSystem, runtime.ctx.tools)


async def test_durable_ack_pending_reconnect_and_approval(tmp_path: Path) -> None:
    provider = ScriptedProvider([call_response("a")])
    runtime = Runtime(
        OathConfig(data_root=tmp_path, credentials={"synthetic-test-token": ("u1", "s1")}),
        loop=asyncio.get_running_loop(),
        tools=[EchoTool("a")],
        policy=VerdictPolicy(default="ask"),
        provider=provider,
    )
    scope = Scope("u1", "s1")
    connections: list[Connection] = []
    handlers: list[asyncio.Task[None]] = []

    async def connect() -> Connection:
        conn = Connection()
        connections.append(conn)
        handlers.append(conn.serve(runtime))
        await conn.command("session.open", "open", {"credential": "synthetic-test-token"})
        await conn.wait_for(lambda f: f["type"] == "ack")
        return conn

    try:
        first = await connect()

        def check_ack(frame: Frame) -> None:
            if frame["type"] == "ack" and frame["id"] == "user-1":
                # ACK 发出瞬间，输入已可从独立 journal reader 恢复。
                from core.journal.runtime_store import RuntimeStore

                assert "user:user-1" in RuntimeStore(runtime.journal).read(scope)["inputs"]

        first.on_send = check_ack
        await first.command("user.message", "user-1", {"content": "do it"})
        await first.wait_for(lambda f: f["type"] == "ack" and f["id"] == "user-1")
        await asyncio.to_thread(until, lambda: tool_system(runtime).permissions.pending(scope))
        await first.close()
        await handlers[0]
        second = await connect()
        await second.command("session.status", "status")
        frame = await second.wait_for(lambda f: f.get("event_type") == "session.status")
        assert frame is not None
        call = frame["payload"]["calls"][0]
        assert call["status"] == "waiting_approval"
        assert len(provider.requests) == 1
        await second.command(
            "permission.decide",
            "approve",
            {"invocation_id": call["invocation_id"], "status": "approved", "decided_by": "forged"},
        )
        await second.wait_for(lambda f: f["type"] == "ack" and f["id"] == "approve")
        await asyncio.to_thread(provider.wait, 2)
        state = runtime.ctx.store.read(scope)
        assert len(state["calls"]) == 1
        assert state["calls"][call["invocation_id"]]["source"] == "system"
        assert state["inputs"]["invoke:approve"]["item"]["payload"]["decided_by"] == "u1"
        with pytest.raises(WireProtocolError):
            AgentApi(runtime).submit(
                Command(
                    "bad",
                    "permission.decide",
                    "s1",
                    {"invocation_id": call["invocation_id"], "status": "approved"},
                ),
                Scope("other-user", "s1"),
            )
    finally:
        for conn in connections:
            await conn.close()
        await asyncio.gather(*handlers)
        await asyncio.to_thread(runtime.close)


async def test_no_command_ack_when_durable_acceptance_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(
        OathConfig(data_root=tmp_path, credentials={"synthetic-test-token": ("u1", "s1")}),
        loop=asyncio.get_running_loop(),
        provider=ScriptedProvider(),
    )
    connection = Connection()
    handler = connection.serve(runtime)
    try:
        await connection.command("session.open", "open", {"credential": "synthetic-test-token"})
        await connection.wait_for(lambda f: f["type"] == "ack")

        def fail(*args: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(runtime.buffers, "accept", fail)
        await connection.command("user.message", "u", {"content": "hello"})
        await connection.wait_for(lambda f: f["type"] == "error" and f["id"] == "u")
        assert not any(f["type"] == "ack" and f["id"] == "u" for f in connection.frames)
        assert not runtime.ctx.store.read(Scope("u1", "s1"))["inputs"]
    finally:
        await connection.close()
        await handler
        await asyncio.to_thread(runtime.close)
