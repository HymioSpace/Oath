"""`EventFeed` —— journal 上的三条内部队列。

重点是两条容易被写错的规则：不推进位置、跳过不属于自己的记录。
"""

from __future__ import annotations

import threading
import time

import pytest

from core.feed import JournalEventFeed
from core.journal.impl import FileJournal
from core.protocol.datatype import (
    Cursor,
    CursorKind,
    EventType,
    Scope,
)
from core.protocol.error import CursorError, UnknownFeedError

# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture
def feed(journal: FileJournal) -> JournalEventFeed:
    return JournalEventFeed(journal)


def _append_user(journal: FileJournal, scope: Scope, key: str, content: str = "hi") -> Cursor:
    return journal.append(
        scope,
        EventType.USER_MESSAGE,
        {"content": content},
        payload_schema_version=1,
        idempotency_key=key,
    ).cursor


def _append_step(journal: FileJournal, scope: Scope, key: str) -> Cursor:
    return journal.append(
        scope,
        EventType.SYSTEM_AGENT_STEP,
        {"step_id": key, "phase": "start"},
        payload_schema_version=1,
        idempotency_key=key,
    ).cursor


# --------------------------------------------------------------------------
# 读与位置
# --------------------------------------------------------------------------


def test_pull_returns_only_the_feed_and_does_not_advance(
    journal: FileJournal, feed: JournalEventFeed, scope: Scope
) -> None:
    _append_user(journal, scope, "u1")
    _append_step(journal, scope, "s1")

    first = feed.pull(scope, "user")

    assert [r.payload["content"] for r in first] == ["hi"]
    # 没 commit 就再 pull 一次，拿到的还是同一条 —— 处理完才算数。
    assert [r.payload["content"] for r in feed.pull(scope, "user")] == ["hi"]

    feed.commit(scope, "user", first[-1].cursor)
    assert feed.pull(scope, "user") == ()


def test_skipped_records_do_not_block_the_queue(
    journal: FileJournal, feed: JournalEventFeed, scope: Scope
) -> None:
    """一条不属于本队列的记录不该把消费方永久卡在它前面。"""
    _append_step(journal, scope, "s1")
    _append_step(journal, scope, "s2")
    _append_user(journal, scope, "u1")

    records = feed.pull(scope, "user")

    assert len(records) == 1
    # 已经跳到第三条，所以 user 队列的位置停在最后一条 step 上，而不是回到起点。
    assert feed.position(scope, "user").value == 2


def test_position_never_steps_over_an_uncommitted_hit(
    journal: FileJournal, feed: JournalEventFeed, scope: Scope
) -> None:
    """命中记录之前的跳过可以推进，命中之后不可以。"""
    _append_step(journal, scope, "s1")  # 1：跳过
    _append_user(journal, scope, "u1")  # 2：命中
    _append_step(journal, scope, "s2")  # 3：跳过，但位置不能越过 2

    first = feed.pull(scope, "user")

    assert feed.position(scope, "user").value == 1
    feed.commit(scope, "user", first[-1].cursor)
    assert feed.position(scope, "user").value == 2
    # 3 被跳过了
    assert feed.pull(scope, "user") == ()


def test_feeds_are_independent(journal: FileJournal, feed: JournalEventFeed, scope: Scope) -> None:
    _append_user(journal, scope, "u1")
    _append_step(journal, scope, "s1")

    assert len(feed.pull(scope, "user")) == 1
    assert len(feed.pull(scope, "system")) == 0


def test_scopes_do_not_leak(journal: FileJournal, feed: JournalEventFeed, scope: Scope) -> None:
    other = Scope(user_id="u1", session_id="s2")
    _append_user(journal, scope, "u1")
    _append_user(journal, other, "u2")

    assert len(feed.pull(scope, "user")) == 1
    assert len(feed.pull(other, "user")) == 1


def test_commit_cannot_go_backward(
    journal: FileJournal, feed: JournalEventFeed, scope: Scope
) -> None:
    cursor = _append_user(journal, scope, "u1")
    feed.commit(scope, "user", cursor)

    with pytest.raises(CursorError):
        feed.commit(scope, "user", Cursor(CursorKind.LINE, 0))


def test_reset_rewinds(feed: JournalEventFeed, journal: FileJournal, scope: Scope) -> None:
    cursor = _append_user(journal, scope, "u1")
    feed.commit(scope, "user", cursor)
    assert feed.pull(scope, "user") == ()

    feed.reset(scope, "user")
    assert len(feed.pull(scope, "user")) == 1


def test_unknown_feed_raises(feed: JournalEventFeed, scope: Scope) -> None:
    with pytest.raises(UnknownFeedError):
        feed.pull(scope, "nope")


# --------------------------------------------------------------------------
# 等待
# --------------------------------------------------------------------------


def test_wait_wakes_on_new_record(feed: JournalEventFeed, scope: Scope) -> None:
    entered = threading.Event()
    woke = threading.Event()

    def waiter() -> None:
        entered.set()
        if feed.wait(scope, "user", timeout=5.0):
            woke.set()

    thread = threading.Thread(target=waiter)
    thread.start()
    assert entered.wait(5.0)
    # 让 waiter 确实进了 wait 再 wake，否则这次 wake 可能在它进入之前就发生，
    # 测试仍然会过，但验的就不是「唤醒」而是「碰巧」。
    time.sleep(0.05)
    feed.wake(scope)

    assert woke.wait(5.0)
    thread.join()


def test_wait_times_out(feed: JournalEventFeed, scope: Scope) -> None:
    started = threading.Event()
    result: list[bool] = []

    def waiter() -> None:
        started.set()
        result.append(feed.wait(scope, "user", timeout=0.05))

    thread = threading.Thread(target=waiter)
    thread.start()
    thread.join()
    assert result == [False]
    assert started.is_set()


def test_wait_survives_a_wake_for_another_scope(feed: JournalEventFeed, scope: Scope) -> None:
    """唤醒按 scope 计数，不是全局广播。"""
    other = Scope(user_id="u1", session_id="s2")
    entered = threading.Event()
    result: list[bool] = []

    def waiter() -> None:
        entered.set()
        result.append(feed.wait(scope, "user", timeout=0.3))

    thread = threading.Thread(target=waiter)
    thread.start()
    entered.wait(5.0)
    time.sleep(0.05)
    feed.wake(other)
    thread.join()
    assert result == [False]


def test_wait_only_returns_once_per_wake(feed: JournalEventFeed, scope: Scope) -> None:
    """第二次 wait 不能被上一次 wake 唤醒 —— 计数只增不减就是为了这个。"""
    feed.wake(scope)
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(feed.wait(scope, "user", timeout=0.05)))
    thread.start()
    thread.join()
    assert result == [False]
