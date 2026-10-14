"""`InMemoryEventQueue` —— 三条内部队列。

重点是三件事：`drain` 一次取光、队列与 scope 的隔离、以及 kind 决定进哪条队列。
"""

from __future__ import annotations

import threading

import pytest

from core.protocol.datatype import Inbound, InboundKind, Scope
from core.protocol.error import UnknownQueueError
from core.queue import InMemoryEventQueue

# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture
def queue() -> InMemoryEventQueue:
    return InMemoryEventQueue()


def _inbound(kind: str, index: int) -> Inbound:
    return Inbound(kind=kind, source=f"src-{index}", source_id=f"id-{index}", occur_dt=f"t{index}")


def _user(index: int) -> Inbound:
    return _inbound(InboundKind.USER, index)


def _tool_result(index: int) -> Inbound:
    return _inbound(InboundKind.TOOL_RESULT, index)


def _ids(items: tuple[Inbound, ...]) -> list[str]:
    return [item.source_id for item in items]


# --------------------------------------------------------------------------
# 入队与出队
# --------------------------------------------------------------------------


def test_drain_returns_items_in_order(queue: InMemoryEventQueue, scope: Scope) -> None:
    queue.put(scope, _user(1))
    queue.put(scope, _user(2))

    assert _ids(queue.drain(scope, "user", limit=64)) == ["id-1", "id-2"]


def test_drain_on_an_empty_queue_returns_nothing(queue: InMemoryEventQueue, scope: Scope) -> None:
    assert queue.drain(scope, "user", limit=64) == ()


def test_dequeue_is_final(queue: InMemoryEventQueue, scope: Scope) -> None:
    """取出即出队。没有位置要推进，也没有「处理完才算数」这回事。"""
    queue.put(scope, _user(1))
    queue.drain(scope, "user", limit=64)

    assert queue.drain(scope, "user", limit=64) == ()
    assert queue.pending(scope, "user") == 0


def test_drain_takes_everything_at_once(queue: InMemoryEventQueue, scope: Scope) -> None:
    """一次取光，不分页 —— 一轮里同时到的结果分批取就是给别的输入插队的机会。"""
    for index in range(5):
        queue.put(scope, _tool_result(index))

    assert _ids(queue.drain(scope, "tool", limit=64)) == [f"id-{i}" for i in range(5)]


def test_limit_caps_a_batch(queue: InMemoryEventQueue, scope: Scope) -> None:
    for index in range(5):
        queue.put(scope, _tool_result(index))

    assert len(queue.drain(scope, "tool", limit=3)) == 3
    assert queue.pending(scope, "tool") == 2


def test_kind_selects_the_queue(queue: InMemoryEventQueue, scope: Scope) -> None:
    queue.put(scope, _user(1))
    queue.put(scope, _tool_result(2))
    queue.put(scope, _inbound(InboundKind.INVOKE, 3))

    assert _ids(queue.drain(scope, "user", limit=64)) == ["id-1"]
    assert _ids(queue.drain(scope, "tool", limit=64)) == ["id-2"]
    assert _ids(queue.drain(scope, "system", limit=64)) == ["id-3"]


def test_queues_do_not_leak_across_scopes(queue: InMemoryEventQueue, scope: Scope) -> None:
    other = Scope(user_id="u1", session_id="s2")
    queue.put(scope, _user(1))
    queue.put(other, _user(2))

    assert _ids(queue.drain(scope, "user", limit=64)) == ["id-1"]
    assert _ids(queue.drain(other, "user", limit=64)) == ["id-2"]


def test_unknown_queue_raises(queue: InMemoryEventQueue, scope: Scope) -> None:
    with pytest.raises(UnknownQueueError):
        queue.drain(scope, "nope", limit=64)


def test_unknown_kind_raises(queue: InMemoryEventQueue, scope: Scope) -> None:
    with pytest.raises(ValueError):
        queue.put(scope, Inbound(kind="nope", source="x"))


# --------------------------------------------------------------------------
# 等待
# --------------------------------------------------------------------------


def test_wait_wakes_on_put(queue: InMemoryEventQueue, scope: Scope) -> None:
    entered = threading.Event()
    result: list[bool] = []

    def waiter() -> None:
        entered.set()
        result.append(queue.wait(scope, timeout=5.0))

    thread = threading.Thread(target=waiter)
    thread.start()
    assert entered.wait(5.0)
    queue.put(scope, _user(1))
    thread.join(5.0)

    assert result == [True]


def test_wait_wakes_for_any_of_the_three_queues(queue: InMemoryEventQueue, scope: Scope) -> None:
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(queue.wait(scope, timeout=5.0)))
    thread.start()
    queue.put(scope, _tool_result(1))
    thread.join(5.0)

    assert result == [True]


def test_wait_times_out_on_an_empty_queue(queue: InMemoryEventQueue, scope: Scope) -> None:
    assert queue.wait(scope, timeout=0.05) is False


def test_wait_ignores_another_scope(queue: InMemoryEventQueue, scope: Scope) -> None:
    other = Scope(user_id="u1", session_id="s2")
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(queue.wait(scope, timeout=0.2)))
    thread.start()
    queue.put(other, _user(1))
    thread.join(5.0)

    assert result == [False]


# --------------------------------------------------------------------------
# 停机
# --------------------------------------------------------------------------


def test_close_unblocks_wait(queue: InMemoryEventQueue, scope: Scope) -> None:
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(queue.wait(scope, timeout=5.0)))
    thread.start()
    queue.close(wait=False)
    thread.join(5.0)

    assert result == [False]


def test_put_after_close_is_dropped(queue: InMemoryEventQueue, scope: Scope) -> None:
    """停机后再入队的东西没有消费方，丢掉 —— 它既没落盘也没持久化。"""
    queue.close(wait=False)
    queue.put(scope, _user(1))

    assert queue.drain(scope, "user", limit=64) == ()
