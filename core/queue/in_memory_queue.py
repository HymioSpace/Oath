"""`InMemoryEventQueue` —— 进程内的三条队列，测试与单机单进程用。"""

from __future__ import annotations

import threading
from collections import deque

from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import check_queue
from core.protocol.datatype.scope import Scope
from core.protocol.interface.queue import EventQueue


class InMemoryEventQueue(EventQueue):
    """每个 `(user_id, session_id, queue)` 一个 deque，一把锁。

    **单进程。** 队列不共享 —— 真实部署里队列是进程外的中间件，这个实现只在测试和
    单进程嵌入时用。它和 Redis 实现遵守同一个接口，换过去不用改调用方。
    """

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str, str], deque[Inbound]] = {}
        self._cond = threading.Condition()
        self._closed = False

    def add(self, scope: Scope, item: Inbound) -> None:
        queue = check_queue(item.kind)
        with self._cond:
            if self._closed:
                return
            key = (scope.user_id, scope.session_id, queue)
            self._buckets.setdefault(key, deque()).append(item)
            self._cond.notify_all()

    def get_all(self, scope: Scope, queue: str) -> tuple[Inbound, ...]:
        check_queue(queue)
        key = (scope.user_id, scope.session_id, queue)
        with self._cond:
            bucket = self._buckets.get(key)
            if not bucket:
                return ()
            taken = tuple(bucket)
            bucket.clear()
            return taken

    def listen(self, scope: Scope, queue: str, timeout: float) -> bool:
        """等到这条队列有东西或者超时。返回 false 表示白等了一轮。"""
        check_queue(queue)
        key = (scope.user_id, scope.session_id, queue)
        with self._cond:
            ready = self._cond.wait_for(
                lambda: bool(self._closed or self._buckets.get(key)),
                timeout=timeout,
            )
            return bool(ready and not self._closed and self._buckets.get(key))

    def close(self, *, wait: bool = True) -> None:
        """停机。队列里的东西随队列一起消失 —— 它们既没落盘也没持久化。"""
        with self._cond:
            self._closed = True
            self._cond.notify_all()


__all__ = ["InMemoryEventQueue"]
