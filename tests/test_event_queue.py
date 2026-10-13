"""`InMemoryEventQueue` —— 三条内部队列。

重点是三件事：入队顺序、队列与 scope 的隔离、以及只有输入型记录才排队。
"""

from __future__ import annotations

import threading

import pytest

from core.protocol.datatype import Cursor, CursorKind, EventRecord, EventType, Record, Scope
from core.protocol.error import UnknownQueueError
from core.queue import InMemoryEventQueue

# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture
def queue() -> InMemoryEventQueue:
    return InMemoryEventQueue()


def _record(type_: str, line: int, *, key: str | None = None) -> Record:
    raw: EventRecord = {
        "user_id": "u1",
        "session_id": "s1",
        "type": type_,
        "payload": {},
        "payload_schema_version": 1,
        "idempotency_key": key or f"{type_}-{line}",
        "occur_dt": None,
        "commit_dt": "2026-10-13T00:00:00Z",
    }
    return Record.of(Cursor(CursorKind.LINE, line), raw)


def _user(line: int) -> Record:
    return _record(EventType.USER_MESSAGE, line)


def _tool_result(line: int) -> Record:
    return _record(EventType.TOOL_RESULT, line)


def _line_of(queue: InMemoryEventQueue, scope: Scope, name: str = "user") -> int:
    """取一条并返回它在日志里的位置。"""
    record = queue.get(scope, name, timeout=0)
    assert record is not None
    return record.cursor.value


# --------------------------------------------------------------------------
# 入队与出队
# --------------------------------------------------------------------------


def test_get_returns_records_in_order(queue: InMemoryEventQueue, scope: Scope) -> None:
    queue.put(scope, _user(1))
    queue.put(scope, _user(2))

    assert _line_of(queue, scope) == 1
    assert _line_of(queue, scope) == 2


def test_get_on_an_empty_queue_returns_none(queue: InMemoryEventQueue, scope: Scope) -> None:
    assert queue.get(scope, "user", timeout=0) is None


def test_dequeue_is_final(queue: InMemoryEventQueue, scope: Scope) -> None:
    """取出即出队。没有位置要推进，也没有「处理完才算数」这回事。"""
    queue.put(scope, _user(1))
    queue.get(scope, "user", timeout=0)

    assert queue.get(scope, "user", timeout=0) is None
    assert queue.pending(scope, "user") == 0


def test_type_selects_the_queue(queue: InMemoryEventQueue, scope: Scope) -> None:
    queue.put(scope, _user(1))
    queue.put(scope, _tool_result(2))

    assert _line_of(queue, scope) == 1
    assert _line_of(queue, scope, "tool") == 2


def test_products_are_not_queued(queue: InMemoryEventQueue, scope: Scope) -> None:
    """`assistant.message` / `tool.call` 是产物：进 journal，但不排队。"""
    queue.put(scope, _record(EventType.ASSISTANT_MESSAGE, 1))
    queue.put(scope, _record(EventType.TOOL_CALL, 2))

    assert queue.pending(scope, "user") == 0
    assert queue.get(scope, "user", timeout=0) is None


def test_queues_do_not_leak_across_scopes(queue: InMemoryEventQueue, scope: Scope) -> None:
    other = Scope(user_id="u1", session_id="s2")
    queue.put(scope, _user(1))
    queue.put(other, _user(2))

    assert _line_of(queue, scope) == 1
    assert _line_of(queue, other) == 2
    assert queue.get(scope, "user", timeout=0) is None


def test_unknown_queue_raises(queue: InMemoryEventQueue, scope: Scope) -> None:
    with pytest.raises(UnknownQueueError):
        queue.get(scope, "nope", timeout=0)


# --------------------------------------------------------------------------
# 等待
# --------------------------------------------------------------------------


def test_get_blocks_until_something_arrives(queue: InMemoryEventQueue, scope: Scope) -> None:
    got: list[Record] = []
    entered = threading.Event()

    def consumer() -> None:
        entered.set()
        record = queue.get(scope, "user", timeout=5.0)
        assert record is not None
        got.append(record)

    thread = threading.Thread(target=consumer)
    thread.start()
    assert entered.wait(5.0)
    queue.put(scope, _user(1))
    thread.join(5.0)

    assert [r.cursor.value for r in got] == [1]


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


def test_close_unblocks_get(queue: InMemoryEventQueue, scope: Scope) -> None:
    result: list[Record | None] = []
    thread = threading.Thread(target=lambda: result.append(queue.get(scope, "user", timeout=5.0)))
    thread.start()
    queue.close(wait=False)
    thread.join(5.0)

    assert result == [None]


def test_close_unblocks_wait(queue: InMemoryEventQueue, scope: Scope) -> None:
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(queue.wait(scope, timeout=5.0)))
    thread.start()
    queue.close(wait=False)
    thread.join(5.0)

    assert result == [False]


def test_put_after_close_is_dropped(queue: InMemoryEventQueue, scope: Scope) -> None:
    """停机后再入队的东西没有消费方，丢掉 —— 它还在 journal 里。"""
    queue.close(wait=False)
    queue.put(scope, _user(1))

    assert queue.get(scope, "user", timeout=0) is None
