"""真实装配与 WebSocket command handler；模型走脚本，socket 走替身。"""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from websockets.asyncio.server import ServerConnection

from bootstrap.agent.agent import Agent
from bootstrap.agent.server import _serve_connection
from core.journal.runtime_store import RuntimeStore
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import Command
from core.protocol.error import WireProtocolError
from core.tool.impl import ThreadedToolSystem
from tests._support.agent_fixtures import CREDENTIAL, SCOPE, agent_config, build
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

    def serve(self, agent: Agent) -> asyncio.Task[None]:
        """按 `_serve_connection` 声明的参数类型起这条连接的处理协程。"""
        return asyncio.create_task(_serve_connection(agent, cast(ServerConnection, self)))


def tool_system(agent: Agent) -> ThreadedToolSystem:
    """装配层放进去的一定是 `ThreadedToolSystem`，`AgentContext.tools` 上只声明了 `ToolSystem`。"""
    return cast(ThreadedToolSystem, agent.ctx.tools)


async def test_ack_then_claim_then_approval(tmp_path: Path) -> None:
    """ACK 只说明「已投进队列」；journal 里出现这条输入要等 Brain 认领。断线重连拿得到全部。"""
    provider = ScriptedProvider([call_response("a")])
    agent = build(
        tmp_path,
        provider=provider,
        tools=[EchoTool("a")],
        policy=VerdictPolicy(default="ask"),
        loop=asyncio.get_running_loop(),
    )
    connections: list[Connection] = []
    handlers: list[asyncio.Task[None]] = []

    async def connect(conn: Connection | None = None) -> Connection:
        conn = conn if conn is not None else Connection()
        connections.append(conn)
        handlers.append(conn.serve(agent))
        await conn.command("session.open", "open", {"credential": CREDENTIAL})
        await conn.wait_for(lambda f: f["type"] == "ack")
        return conn

    try:
        agent.start()
        first = await connect()

        def check_ack(frame: Frame) -> None:
            if frame["type"] == "ack" and frame["id"] == "user-1":
                # ACK 发出瞬间还没有 inputs —— 认领还没发生，这正是新契约。
                assert RuntimeStore(agent.journal).read(SCOPE)["inputs"] == {}

        first.on_send = check_ack
        await first.command("user.message", "user-1", {"content": "do it", "user_context": {}})
        await first.wait_for(lambda f: f["type"] == "ack" and f["id"] == "user-1")
        await asyncio.to_thread(until, lambda: tool_system(agent).permissions.pending(SCOPE))
        assert "user:user-1" in RuntimeStore(agent.journal).read(SCOPE)["inputs"]

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
        state = agent.ctx.store.read(SCOPE)
        assert len(state["calls"]) == 1
        assert state["calls"][call["invocation_id"]]["source"] == "system"
        # 控制命令当场生效，不进 buffer —— 它在 journal 里的位置是 controls。
        assert state["controls"]["approve"]["identity"]["decision"] == "approved"
        assert state["controls"]["approve"]["applied"] is True

        with pytest.raises(WireProtocolError):
            agent.submit(
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
        await asyncio.to_thread(agent.close)


async def test_an_undeliverable_command_gets_no_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """投递失败要报错帧，不发 ACK —— ACK 的含义是「队列收到了」，答不到就不能说收到了。"""
    connection = Connection()
    agent = build(tmp_path, loop=asyncio.get_running_loop())
    handler = connection.serve(agent)
    try:
        await connection.command("session.open", "open", {"credential": CREDENTIAL})
        await connection.wait_for(lambda f: f["type"] == "ack")

        def fail(scope: Scope, item: Any) -> None:
            raise OSError("queue unavailable")

        monkeypatch.setattr(agent.ctx.queue, "add", fail)
        await connection.command("user.message", "u", {"content": "hello", "user_context": {}})
        await connection.wait_for(lambda f: f["type"] == "error" and f["id"] == "u")
        assert not any(f["type"] == "ack" and f["id"] == "u" for f in connection.frames)
        assert agent.ctx.store.read(SCOPE)["inputs"] == {}
    finally:
        await connection.close()
        await handler
        await asyncio.to_thread(agent.close)


async def test_a_command_for_another_session_is_refused(tmp_path: Path) -> None:
    connection = Connection()
    agent = build(tmp_path, loop=asyncio.get_running_loop())
    handler = connection.serve(agent)
    try:
        await connection.command("session.open", "open", {"credential": CREDENTIAL})
        await connection.wait_for(lambda f: f["type"] == "ack")
        await connection.incoming.put(
            {"v": 1, "type": "user.message", "id": "u", "session_id": "other", "payload": {}}
        )
        await connection.wait_for(lambda f: f["type"] == "error")
    finally:
        await connection.close()
        await handler
        await asyncio.to_thread(agent.close)


def test_the_configuration_has_no_credential_values(tmp_path: Path) -> None:
    """配置里只有环境变量名。序列化出去的配置里不该出现任何密钥。"""
    import json

    rendered = json.dumps(agent_config(tmp_path).to_json(), ensure_ascii=False)

    assert "credential" not in rendered
    assert CREDENTIAL not in rendered
