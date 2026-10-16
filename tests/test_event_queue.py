"""`InMemoryEventQueue` —— `EventQueue` 的语义：取走即出队、按 kind 分队列、按 scope 隔离。

**同一个接口有两份实现**（这里与 `RedisEventQueue`），所以这里测的是接口承诺的行为，
不是某个中间件的细节 —— Redis 那份要另测它自己独有的部分（消费组、延迟 ack）。
"""

from __future__ import annotations

import threading

import pytest

from core.protocol.datatype import Inbound, Scope
from core.protocol.datatype.queue import QueueName
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
    return _inbound(QueueName.USER, index)


def _tool_result(index: int) -> Inbound:
    return _inbound(QueueName.TOOL, index)


def _ids(items: tuple[Inbound, ...]) -> list[str]:
    return [item.source_id for item in items]


# --------------------------------------------------------------------------
# 入队与出队
# --------------------------------------------------------------------------


def test_get_all_returns_items_in_order(queue: InMemoryEventQueue, scope: Scope) -> None:
    queue.add(scope, _user(1))
    queue.add(scope, _user(2))

    assert _ids(queue.get_all(scope, "user")) == ["id-1", "id-2"]


def test_get_all_on_an_empty_queue_returns_nothing(queue: InMemoryEventQueue, scope: Scope) -> None:
    assert queue.get_all(scope, "user") == ()


def test_get_all_is_final(queue: InMemoryEventQueue, scope: Scope) -> None:
    """取走即出队。没有位置要推进，也没有「处理完才算数」这回事。"""
    queue.add(scope, _user(1))
    queue.get_all(scope, "user")

    assert queue.get_all(scope, "user") == ()


def test_get_all_takes_everything_at_once(queue: InMemoryEventQueue, scope: Scope) -> None:
    """一次取光，不分页 —— 一轮里同时到的结果分批取就是给别的输入插队的机会。"""
    for index in range(5):
        queue.add(scope, _tool_result(index))

    assert _ids(queue.get_all(scope, "tool")) == [f"id-{i}" for i in range(5)]


def test_kind_selects_the_queue(queue: InMemoryEventQueue, scope: Scope) -> None:
    queue.add(scope, _user(1))
    queue.add(scope, _tool_result(2))
    queue.add(scope, _inbound(QueueName.SYSTEM, 3))

    assert _ids(queue.get_all(scope, "user")) == ["id-1"]
    assert _ids(queue.get_all(scope, "tool")) == ["id-2"]
    assert _ids(queue.get_all(scope, "system")) == ["id-3"]


def test_queues_do_not_leak_across_scopes(queue: InMemoryEventQueue, scope: Scope) -> None:
    other = Scope(user_id="u1", session_id="s2")
    queue.add(scope, _user(1))
    queue.add(other, _user(2))

    assert _ids(queue.get_all(scope, "user")) == ["id-1"]
    assert _ids(queue.get_all(other, "user")) == ["id-2"]


def test_unknown_queue_raises(queue: InMemoryEventQueue, scope: Scope) -> None:
    with pytest.raises(UnknownQueueError):
        queue.get_all(scope, "nope")


def test_unknown_kind_raises(queue: InMemoryEventQueue, scope: Scope) -> None:
    with pytest.raises(UnknownQueueError):
        queue.add(scope, Inbound(kind="nope", source="x"))


# --------------------------------------------------------------------------
# 等待
# --------------------------------------------------------------------------


def test_listen_wakes_on_add(queue: InMemoryEventQueue, scope: Scope) -> None:
    entered = threading.Event()
    result: list[bool] = []

    def waiter() -> None:
        entered.set()
        result.append(queue.listen(scope, "user", 5.0))

    thread = threading.Thread(target=waiter)
    thread.start()
    assert entered.wait(5.0)
    queue.add(scope, _user(1))
    thread.join(5.0)

    assert result == [True]


def test_listen_only_watches_its_own_queue(queue: InMemoryEventQueue, scope: Scope) -> None:
    """`listen` 按队列名分别订阅 —— system 有东西不能让 user 的 consumer 醒。"""
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(queue.listen(scope, "user", 0.2)))
    thread.start()
    queue.add(scope, _tool_result(1))
    thread.join(5.0)

    assert result == [False]


def test_listen_times_out_on_an_empty_queue(queue: InMemoryEventQueue, scope: Scope) -> None:
    assert queue.listen(scope, "user", 0.05) is False


def test_listen_ignores_another_scope(queue: InMemoryEventQueue, scope: Scope) -> None:
    other = Scope(user_id="u1", session_id="s2")
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(queue.listen(scope, "user", 0.2)))
    thread.start()
    queue.add(other, _user(1))
    thread.join(5.0)

    assert result == [False]


# --------------------------------------------------------------------------
# 停机
# --------------------------------------------------------------------------


def test_close_unblocks_listen(queue: InMemoryEventQueue, scope: Scope) -> None:
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(queue.listen(scope, "user", 5.0)))
    thread.start()
    queue.close(wait=False)
    thread.join(5.0)

    assert result == [False]


def test_add_after_close_is_dropped(queue: InMemoryEventQueue, scope: Scope) -> None:
    """停机后再入队的东西没有消费方，丢掉 —— 它既没落盘也没持久化。"""
    queue.close(wait=False)
    queue.add(scope, _user(1))

    assert queue.get_all(scope, "user") == ()
