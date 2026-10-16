"""Supervisor 的两件事：定期投 `system.invoke`，agent 不在了请 manager 重启。

不启动线程 —— 直接调那两轮工作，判定逻辑才看得见。时间用手控的 monotonic。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from bootstrap.supervisor.watchdog import ScopeSupervisor
from core.protocol.datatype.config import SupervisorConfig
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import QueueName
from core.protocol.datatype.scope import Scope
from core.protocol.interface.queue import EventQueue

SCOPE = Scope("u1", "s1")


class RecordingQueue(EventQueue):
    def __init__(self, *, fail: bool = False) -> None:
        self.added: list[Inbound] = []
        self.fail = fail

    def add(self, scope: Scope, item: Inbound) -> None:
        if self.fail:
            raise OSError("queue unavailable")
        assert scope == SCOPE
        self.added.append(item)

    def get_all(self, scope: Scope, queue: str) -> tuple[Inbound, ...]:
        return ()

    def listen(self, scope: Scope, queue: str, timeout: float) -> bool:
        return False

    def close(self, *, wait: bool = True) -> None:
        pass


class Pipe:
    """`multiprocessing.Pipe` 的那一半。记录发出去的重启请求，也模拟发送失败。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def send(self, message: dict[str, Any]) -> None:
        if self.closed:
            raise OSError("manager gone")
        self.sent.append(message)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """退避靠 monotonic 推进，测试里不真等。"""
    monkeypatch.setattr(time, "monotonic", lambda: _CLOCK[0])


_CLOCK = [0.0]


def build(queue: RecordingQueue, control: Pipe, **policy: Any) -> ScopeSupervisor:
    defaults: dict[str, Any] = {
        "heartbeat_failures_before_restart": 2,
        "heartbeat_timeout_seconds": 1.0,
        "restart_backoff_seconds": 1.0,
        "restart_backoff_cap_seconds": 8.0,
    }
    return ScopeSupervisor(
        SupervisorConfig(**{**defaults, **policy}),
        queue,  # type: ignore[arg-type]
        SCOPE,
        "ws://127.0.0.1:65535",
        control,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# 强制唤醒
# --------------------------------------------------------------------------


def test_invoke_produces_a_system_event() -> None:
    """投的是一条事件，不是一次调用 —— 它自己不是输入，不该进 buffer。"""
    queue = RecordingQueue()

    build(queue, Pipe())._invoke(0.0)

    assert [item.kind for item in queue.added] == [QueueName.SYSTEM]
    assert queue.added[0].source == "system.invoke"
    assert queue.added[0].payload == {}


def test_each_invoke_is_distinct() -> None:
    """每次都是新的一条。source_id 相同的话，journal 认领时会把它当重投丢掉。"""
    queue = RecordingQueue()
    supervisor = build(queue, Pipe())

    supervisor._invoke(0.0)
    _CLOCK[0] += 1
    supervisor._invoke(0.0)

    assert len({item.source_id for item in queue.added}) == 2


def test_an_unavailable_queue_is_not_swallowed() -> None:
    """投不出去就让它抛出去 —— `Periodic` 退避重试，假装成功等于静默丢掉唤醒。"""
    supervisor = build(RecordingQueue(fail=True), Pipe())

    with pytest.raises(OSError):
        supervisor._invoke(0.0)


# --------------------------------------------------------------------------
# 看住 agent
# --------------------------------------------------------------------------


def patch_alive(monkeypatch: pytest.MonkeyPatch, alive: bool) -> None:
    monkeypatch.setattr("bootstrap.supervisor.watchdog._alive", lambda url, timeout: alive)


def test_a_healthy_agent_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    control = Pipe()
    supervisor = build(RecordingQueue(), control)
    patch_alive(monkeypatch, True)

    for _ in range(5):
        _CLOCK[0] += 10
        supervisor._check(0.0)

    assert control.sent == []


def cross_threshold(supervisor: ScopeSupervisor, *, after: float = 10.0) -> None:
    """连续失败到阈值，再跨过退避 —— 这之后才该发出重启请求。"""
    supervisor._check(0.0)
    _CLOCK[0] += after
    supervisor._check(0.0)
    _CLOCK[0] += after
    supervisor._check(0.0)


def test_restart_is_asked_for_only_after_the_failure_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = Pipe()
    supervisor = build(RecordingQueue(), control)
    patch_alive(monkeypatch, False)

    supervisor._check(0.0)
    assert control.sent == []
    cross_threshold(supervisor)

    assert control.sent == [{"action": "restart_agent", "user_id": "u1", "session_id": "s1"}]


def test_the_request_identifies_the_owning_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """一个 supervisor 只看一个 scope —— 请求里必须带得出是谁。"""
    control = Pipe()
    supervisor = build(RecordingQueue(), control)
    patch_alive(monkeypatch, False)

    cross_threshold(supervisor)

    assert control.sent[0]["session_id"] == SCOPE.session_id


def test_a_restart_inside_the_backoff_is_not_repeated(monkeypatch: pytest.MonkeyPatch) -> None:
    control = Pipe()
    supervisor = build(RecordingQueue(), control)
    patch_alive(monkeypatch, False)

    cross_threshold(supervisor)
    assert len(control.sent) == 1

    # 请求刚发出去，退避还没走完 —— 再失败也不该连着发。
    _CLOCK[0] += 1
    supervisor._check(0.0)
    assert len(control.sent) == 1

    # agent 还是没起来。失败计数已经清零，要重新走一遍阈值与退避。
    cross_threshold(supervisor)
    assert len(control.sent) == 2


def test_recovery_resets_the_failure_run(monkeypatch: pytest.MonkeyPatch) -> None:
    control = Pipe()
    supervisor = build(RecordingQueue(), control)
    patch_alive(monkeypatch, False)
    supervisor._check(0.0)
    patch_alive(monkeypatch, True)
    _CLOCK[0] += 10
    supervisor._check(0.0)
    patch_alive(monkeypatch, False)

    supervisor._check(0.0)
    assert control.sent == []

    cross_threshold(supervisor)
    assert len(control.sent) == 1


def test_an_unreachable_manager_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    """发不出去要记下来说明没有人拉起 agent，不能当成已经请求过了。"""
    control = Pipe()
    control.closed = True
    supervisor = build(RecordingQueue(), control)
    patch_alive(monkeypatch, False)

    cross_threshold(supervisor)

    assert supervisor._restarts == 1
