"""Supervisor 唯一的事：按周期往 system 队列投 `system.invoke`。

不启动线程 —— 直接调那一轮工作，判定逻辑才看得见。
"""

from __future__ import annotations

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

    def confirm(self, scope: Scope, queue: str, item: Inbound) -> None:
        pass

    def listen(self, scope: Scope, queue: str, timeout: float) -> bool:
        return False

    def close(self, *, wait: bool = True) -> None:
        pass


def build(queue: RecordingQueue, **policy: object) -> ScopeSupervisor:
    return ScopeSupervisor(
        SupervisorConfig(**policy),  # type: ignore[arg-type]
        queue,  # type: ignore[arg-type]
        SCOPE,
    )


def test_invoke_produces_a_system_event() -> None:
    """投的是一条事件，不是一次调用 —— 它自己不是输入，不该进 buffer。"""
    queue = RecordingQueue()

    build(queue)._invoke(0.0)

    assert [item.kind for item in queue.added] == [QueueName.SYSTEM]
    assert queue.added[0].source == "system.invoke"
    assert queue.added[0].payload == {}


def test_each_invoke_is_distinct() -> None:
    """每次都是新的一条。source_id 相同的话，journal 认领时会把它当重投丢掉。"""
    queue = RecordingQueue()
    supervisor = build(queue)

    supervisor._invoke(0.0)
    supervisor._invoke(0.0)

    assert len({item.source_id for item in queue.added}) == 2


def test_an_unavailable_queue_is_not_swallowed() -> None:
    """投不出去就让它抛出去 —— `Periodic` 退避重试，假装成功等于静默丢掉唤醒。"""
    supervisor = build(RecordingQueue(fail=True))

    with pytest.raises(OSError):
        supervisor._invoke(0.0)


def test_the_period_is_configurable() -> None:
    """间隔与投递上限都要短于彼此，否则慢投递会连着慢两轮。"""
    with pytest.raises(ValueError, match="invoke_interval_seconds"):
        SupervisorConfig(invoke_interval_seconds=1.0, invoke_timeout_seconds=2.0)

    with pytest.raises(ValueError, match="invoke_timeout_seconds"):
        SupervisorConfig(invoke_interval_seconds=2.0, invoke_timeout_seconds=float("inf"))
