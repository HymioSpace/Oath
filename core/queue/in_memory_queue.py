"""`InMemoryEventQueue` —— 内存里的三条队列。"""

from __future__ import annotations

import threading
from collections import deque

from core.protocol.datatype.inbound import Inbound
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
        self._buckets: dict[tuple[str, str, str], deque[Inbound]] = {}
        self._cond = threading.Condition()
        self._closed = False

    # -- 入队 --------------------------------------------------------------

    def put(self, scope: Scope, item: Inbound) -> None:
        with self._cond:
            if self._closed:
                return
            key = (scope.user_id, scope.session_id, queue_of(item.kind))
            self._buckets.setdefault(key, deque()).append(item)
            self._cond.notify_all()

    # -- 出队 --------------------------------------------------------------

    def drain(self, scope: Scope, queue: str, *, limit: int = 64) -> tuple[Inbound, ...]:
        if queue not in QueueName.ALL:
            raise UnknownQueueError(queue)
        key = (scope.user_id, scope.session_id, queue)
        with self._cond:
            bucket = self._buckets.get(key)
            if not bucket:
                return ()
            count = min(limit, len(bucket))
            drained = tuple(bucket.popleft() for _ in range(count))
            return drained

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
                # 判空与等待在同一把锁里，所以不会漏掉 put：put 要拿同一把锁。
                if not self._cond.wait(timeout):
                    return False
            return True

    def _has_pending_locked(self, scope: Scope) -> bool:
        return any(
            self._buckets.get((scope.user_id, scope.session_id, queue)) for queue in QueueName.ALL
        )

    # -- 生命周期 ----------------------------------------------------------

    def close(self, *, wait: bool = True) -> None:
        """停机。队列里的东西随队列一起消失 —— 它们既没落盘也没持久化。"""
        with self._cond:
            self._closed = True
            self._cond.notify_all()


__all__ = ["InMemoryEventQueue"]
