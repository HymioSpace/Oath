"""`InMemoryEventQueue` —— 内存里的三条队列。"""

from __future__ import annotations

import threading
from collections import deque

from core.protocol.datatype.journal import Record
from core.protocol.datatype.queue import QueueName, queue_of
from core.protocol.datatype.scope import Scope
from core.protocol.error import UnknownQueueError
from core.protocol.interface.queue import EventQueue


class InMemoryEventQueue(EventQueue):
    """每个 `(user_id, session_id, queue)` 一个 deque，一把锁。

    **单进程。** 队列不共享，多个进程各有各的 —— v1 就是单进程 runtime，不为还不存在的
    部署形态付分布式队列的代价。
    """

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str, str], deque[Record]] = {}
        self._cond = threading.Condition()
        self._closed = False

    # -- 入队 --------------------------------------------------------------

    def put(self, scope: Scope, record: Record) -> None:
        queue = queue_of(record.type)
        if queue is None:
            return
        with self._cond:
            if self._closed:
                return
            key = (scope.user_id, scope.session_id, queue)
            self._buckets.setdefault(key, deque()).append(record)
            self._cond.notify_all()

    # -- 出队 --------------------------------------------------------------

    def get(self, scope: Scope, queue: str, *, timeout: float | None = None) -> Record | None:
        if queue not in QueueName.ALL:
            raise UnknownQueueError(queue)
        key = (scope.user_id, scope.session_id, queue)
        with self._cond:
            while True:
                bucket = self._buckets.get(key)
                if bucket:
                    return bucket.popleft()
                if self._closed:
                    return None
                # 判空与等待在同一把锁里，所以不会漏掉 put：put 要拿同一把锁。
                if not self._cond.wait(timeout):
                    return None

    def pending(self, scope: Scope, queue: str) -> int:
        if queue not in QueueName.ALL:
            raise UnknownQueueError(queue)
        with self._cond:
            bucket = self._buckets.get((scope.user_id, scope.session_id, queue))
            return len(bucket) if bucket else 0

    def wait(self, scope: Scope, *, timeout: float | None = None) -> bool:
        with self._cond:
            while not self._has_pending_locked(scope):
                if self._closed:
                    return False
                if not self._cond.wait(timeout):
                    return False
            return True

    def _has_pending_locked(self, scope: Scope) -> bool:
        return any(
            self._buckets.get((scope.user_id, scope.session_id, queue)) for queue in QueueName.ALL
        )

    # -- 生命周期 ----------------------------------------------------------

    def close(self, *, wait: bool = True) -> None:
        """停机。队列里的记录随队列一起消失 —— 它们还在 journal 里。"""
        with self._cond:
            self._closed = True
            self._cond.notify_all()


__all__ = ["InMemoryEventQueue"]
