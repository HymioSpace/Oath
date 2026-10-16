"""manager 管两个进程，但**每个进程只有一个主人**：agent 归 supervisor，supervisor 归
manager。两条重启路分开走，谁也不越界去动对方那个。

被越界的代价很具体：换 supervisor 时顺带重拉 agent，会在同一个端口上多出一个抢
ScopeLock 的进程；换 agent 时顺带造一个 supervisor，会留下一个没人管的孤儿 ——
它继续跑自己的周期任务，拿着一条读者端已关的控制道，把每次心跳失败都记成
「重启请求发不出去」。
"""

from __future__ import annotations

from typing import Any

import pytest

from bootstrap.manager import manager as manager_module
from bootstrap.manager.manager import SessionEntry, SessionManager
from core.protocol.datatype.config import OathConfig
from core.protocol.datatype.scope import Scope

SCOPE = Scope("u1", "abcdef0123456789")


class FakeProcess:
    """`multiprocessing.Process` 的替身。只记自己有没有被停过。"""

    def __init__(self, *, target: Any = None, args: tuple[Any, ...] = (), **_kw: Any) -> None:
        self.name = f"fake-{id(self)}"
        self._alive = True
        self.stopped = False

    def start(self) -> None:
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.stopped = True
        self._alive = False

    def kill(self) -> None:
        self.terminate()

    def join(self, timeout: float | None = None) -> None:
        return None

    def die(self) -> None:
        """模拟这个进程自己没了。"""
        self._alive = False


class FakeAgent:
    """agent 子进程的替身。"""

    def __init__(self, cmd: list[str]) -> None:
        self.cmd = cmd
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode


class FakePipeEnd:
    def close(self) -> None:
        return None


class FakePipe:
    """控制道。`poll` 返回有没有待读的消息 —— `None` / `False` 就是没有。"""

    def __init__(self) -> None:
        self.closed = False
        self.inbox: list[dict[str, Any]] = []

    def poll(self, timeout: float = 0) -> bool:
        return bool(self.inbox)

    def recv(self) -> dict[str, Any]:
        return self.inbox.pop(0)

    def send(self, message: dict[str, Any]) -> None:
        self.inbox.append(message)

    def close(self) -> None:
        self.closed = True


class FakeContext:
    """造出来的 supervisor 都记在这里，测试要靠它问「现在有几个」。"""

    def __init__(self) -> None:
        self.processes: list[FakeProcess] = []

    def Pipe(self, *, duplex: bool = False) -> tuple[FakePipe, FakePipeEnd]:
        return FakePipe(), FakePipeEnd()

    def Process(self, *, target: Any = None, args: tuple[Any, ...] = (), **kw: Any) -> FakeProcess:
        process = FakeProcess(target=target, args=args, **kw)
        self.processes.append(process)
        return process

    @property
    def live(self) -> list[FakeProcess]:
        return [p for p in self.processes if p.is_alive()]


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """manager 装好替身：一个 entry，一个活着但没人管的 agent 替身。"""
    started: list[list[str]] = []

    class FakePopen(FakeAgent):
        def __init__(self, cmd: list[str], **_kw: Any) -> None:
            super().__init__(cmd)
            started.append(cmd)

    monkeypatch.setattr("subprocess.Popen", FakePopen)
    monkeypatch.setattr(manager_module, "_probe_connections", lambda entry, timeout: 1)
    monkeypatch.setattr(manager_module, "_supervise", lambda *a: None)

    session = SessionManager(OathConfig(), "oath.toml")
    context = FakeContext()
    session._context = context  # type: ignore[assignment]
    entry = SessionEntry(SCOPE, 9001, "credential")
    session._sessions[SCOPE] = entry
    return {"manager": session, "entry": entry, "context": context, "started": started}


def test_an_agent_restart_leaves_the_supervisor_alone(wired: dict[str, Any]) -> None:
    """agent 归 supervisor 管。manager 只是替它执行，不能顺手把 supervisor 也换掉。"""
    manager, entry, context = wired["manager"], wired["entry"], wired["context"]
    manager._start_supervisor(entry)
    supervisor = entry.supervisor
    assert context.live == [supervisor]

    entry.agent = FakeAgent(["agent"])  # agent 确实没了
    entry.agent.returncode = -9
    entry.control.inbox.append(
        {"action": "restart_agent", "user_id": SCOPE.user_id, "session_id": SCOPE.session_id}
    )
    manager._restart_requested(entry)

    assert entry.supervisor is supervisor
    assert len(wired["started"]) == 1, "重启 agent 只该拉一个 agent"


def test_a_dead_supervisor_is_replaced_without_touching_the_agent(wired: dict[str, Any]) -> None:
    """supervisor 归 manager 管，agent 不参与。换个 supervisor 不该动 agent。"""
    manager, entry, context = wired["manager"], wired["entry"], wired["context"]
    manager._start_agent(entry)
    manager._start_supervisor(entry)
    agent, first = entry.agent, entry.supervisor
    first.die()

    manager._reap_round()

    assert entry.supervisor is not first
    assert context.live == [entry.supervisor], "旧 supervisor 没人管就是孤儿"
    assert entry.agent is agent, "agent 有 supervisor 这一个主人，manager 越过它就是两个主人"
    assert len(wired["started"]) == 1


def test_replacing_a_live_supervisor_stops_it_first(wired: dict[str, Any]) -> None:
    """换之前先停旧的 —— 不留一个拿着死管道的孤儿。"""
    manager, entry, context = wired["manager"], wired["entry"], wired["context"]
    manager._start_supervisor(entry)
    first = entry.supervisor

    manager._start_supervisor(entry)

    assert first.stopped, "旧的 supervisor 必须先停"
    assert len(context.live) == 1
