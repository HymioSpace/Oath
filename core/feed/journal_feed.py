"""`JournalEventFeed` —— `EventFeed` 的 v1 实现。

位置是一张内存里的 `dict[(user_id, session_id, feed), Cursor]`，一把 `Condition`
负责唤醒。位置**不落盘**：v1 单进程，重启后回到起点、事件重放一遍，重放安全是因为
fold 是纯函数。要持久化时它本身也是一条 journal 记录，不是另开一个存储。

同一把锁保护位置表与唤醒，避免「push 了位置才 notify」这种次序错误。
"""

from __future__ import annotations

import threading
import time

from core.journal.cursor import beginning, check_kind
from core.protocol.datatype.cursor import Cursor
from core.protocol.datatype.feed import FeedName, belongs_to
from core.protocol.datatype.journal import Record
from core.protocol.datatype.scope import Scope
from core.protocol.error import CursorError, UnknownFeedError
from core.protocol.interface.feed import EventFeed
from core.protocol.interface.journal import Journal


class JournalEventFeed(EventFeed):
    """journal 之上的游标视图。"""

    def __init__(self, journal: Journal) -> None:
        self._journal = journal
        self._lock = threading.Condition()
        self._positions: dict[tuple[str, str, str], Cursor] = {}
        #: 每个 scope 的唤醒计数。不用 Event 对象是因为一个 Event 被 notify 之后
        #: 就复位，两个 wait 者会互相唤醒不到 —— 计数只增不减才能正确判「有没有
        #: 在我等的时候发生过」。
        self._wakes: dict[tuple[str, str], int] = {}

    @property
    def journal(self) -> Journal:
        return self._journal

    # -- 读 ---------------------------------------------------------------

    def pull(self, scope: Scope, feed: str, *, limit: int | None = None) -> tuple[Record, ...]:
        _check_feed(feed)
        key = (scope.user_id, scope.session_id, feed)
        with self._lock:
            start = self._positions.get(key) or beginning(self._journal.cursor_kind)
            kept: list[Record] = []
            # 位置只能推到「最后一条被跳过的记录」，一旦命中就不再推进：越过第一条
            # 命中的记录会让它永远读不到。
            boundary = start
            for record in self._journal.read_range(scope, start, limit=limit):
                if belongs_to(feed, record.type):
                    kept.append(record)
                elif not kept:
                    boundary = record.cursor
            if boundary != start:
                self._positions[key] = boundary
            return tuple(kept)

    def commit(self, scope: Scope, feed: str, cursor: Cursor) -> None:
        _check_feed(feed)
        check_kind(cursor, frozenset({self._journal.cursor_kind}))
        key = (scope.user_id, scope.session_id, feed)
        with self._lock:
            current = self._positions.get(key) or beginning(self._journal.cursor_kind)
            if cursor.value < current.value:
                raise CursorError(
                    f"{feed} 的位置只能前进：当前 {current.value}，收到 {cursor.value}"
                )
            self._positions[key] = cursor

    def position(self, scope: Scope, feed: str) -> Cursor:
        _check_feed(feed)
        key = (scope.user_id, scope.session_id, feed)
        with self._lock:
            return self._positions.get(key) or beginning(self._journal.cursor_kind)

    # -- 等待与唤醒 --------------------------------------------------------

    def wake(self, scope: Scope) -> None:
        """记录一次「新记录落进来了」。计数只增不减。"""
        with self._lock:
            key = (scope.user_id, scope.session_id)
            self._wakes[key] = self._wakes.get(key, 0) + 1
            self._lock.notify_all()

    def wait(self, scope: Scope, feed: str, *, timeout: float | None = None) -> bool:
        _check_feed(feed)
        scope_key = (scope.user_id, scope.session_id)
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            seen = self._wakes.get(scope_key, 0)
            while self._wakes.get(scope_key, 0) == seen:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0.0:
                    return False
                self._lock.wait(remaining)
        return True

    # -- 复位 --------------------------------------------------------------

    def reset(self, scope: Scope, feed: str | None = None) -> None:
        if feed is not None:
            _check_feed(feed)
        start = beginning(self._journal.cursor_kind)
        with self._lock:
            for key in [
                k
                for k in self._positions
                if k[0] == scope.user_id
                and k[1] == scope.session_id
                and (feed is None or k[2] == feed)
            ]:
                self._positions[key] = start


def _check_feed(feed: str) -> None:
    if feed not in FeedName.ALL:
        raise UnknownFeedError(f"feed 名字 {feed!r} 不认识，可用: {sorted(FeedName.ALL)}")


__all__ = ["JournalEventFeed"]
