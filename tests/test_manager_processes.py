"""manager 管两个进程，但**每个进程只有一个主人**：换 supervisor 时不许顺带重拉 agent。

被越界的代价很具体：换 supervisor 时顺带重拉 agent，会在同一个端口上多出一个抢
ScopeLock 的进程。

另一条边界是回收只看连接数：探不到 agent 不等于该回收它。
"""

from __future__ import annotations

from dataclasses import replace
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


class FakeContext:
    """造出来的 supervisor 都记在这里，测试要靠它问「现在有几个」。"""

    def __init__(self) -> None:
        self.processes: list[FakeProcess] = []

    def Process(self, *, target: Any = None, args: tuple[Any, ...] = (), **kw: Any) -> FakeProcess:
        process = FakeProcess(target=target, args=args, **kw)
        self.processes.append(process)
        return process

    @property
    def live(self) -> list[FakeProcess]:
        return [p for p in self.processes if p.is_alive()]


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """manager 装好替身：一个 entry。"""
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


def test_a_dead_supervisor_is_replaced_without_touching_the_agent(wired: dict[str, Any]) -> None:
    """换个 supervisor 不该动 agent —— manager 越过 supervisor 去动就是两个主人。"""
    manager, entry, context = wired["manager"], wired["entry"], wired["context"]
    manager._start_agent(entry)
    manager._start_supervisor(entry)
    agent, first = entry.agent, entry.supervisor
    first.die()

    manager._reap_round()

    assert entry.supervisor is not first
    assert context.live == [entry.supervisor], "旧 supervisor 没人管就是孤儿"
    assert entry.agent is agent, "manager 越过 supervisor 去动 agent 就是两个主人"
    assert len(wired["started"]) == 1


def test_an_unreachable_agent_is_left_alone(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """agent 死了不回收 —— 只回收「还探得到、但没人连」的 scope。

    回收一个探不到的 agent 等于删掉一个可能只是忙着的端口；它连不上这件事，说明不了
    它已经不在工作。
    """
    manager, entry = wired["manager"], wired["entry"]
    manager._start_agent(entry)
    manager._start_supervisor(entry)
    manager._config = replace(
        manager._config,
        manager=replace(manager._config.manager, idle_grace_seconds=0.001),
    )
    monkeypatch.setattr(manager_module, "_probe_connections", lambda e, timeout: None)

    manager._reap_round()
    manager._reap_round()

    assert entry.agent is not None, "探不到 agent 不等于该回收它"
    assert SCOPE in manager._sessions


def test_replacing_a_live_supervisor_stops_it_first(wired: dict[str, Any]) -> None:
    """换之前先停旧的 —— 不留一个拿着死管道的孤儿。"""
    manager, entry, context = wired["manager"], wired["entry"], wired["context"]
    manager._start_supervisor(entry)
    first = entry.supervisor

    manager._start_supervisor(entry)

    assert first.stopped, "旧的 supervisor 必须先停"
    assert len(context.live) == 1
