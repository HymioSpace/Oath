"""`DiskEventBuffer` —— 待认领输入的持久存放。

一条 `(scope, 队列)` 一个键，值是这一组当前全部输入的 JSON 列表。`peek` 读，`remove`
删 —— **没有「读走」这回事**：读和删分成两步，中间那一步崩溃时输入还在。

一个 data_root 下跑着多个 agent 进程，键里必须带 scope：只有队列名的话，A 的认领会
取走 B 的输入。
"""

from __future__ import annotations

import threading
from pathlib import Path

from diskcache import Cache

from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import check_queue
from core.protocol.datatype.scope import Scope
from core.protocol.interface.buffer import EventBuffer


class DiskEventBuffer(EventBuffer):
    """本地文件里的待认领输入。进程崩了重启还在 —— 它是输入可靠性的来源。

    **同一进程里 consumer 与 Brain 并发读写同一个键**：三个 consumer 各自 add 自己那条
    队列，Brain 同时在 remove。diskcache 自己按 SQLite 事务串行化单条语句，但
    「读出来 → 改 → 写回去」这三步之间留了缝，所以这里另加一把进程内的锁。
    """

    def __init__(self, directory: Path) -> None:
        self._cache = Cache(str(directory))
        self._lock = threading.Lock()

    @staticmethod
    def _key(scope: Scope, queue: str) -> str:
        return f"{scope.user_id}:{scope.session_id}:{check_queue(queue)}"

    def add(self, scope: Scope, queue: str, item: Inbound) -> None:
        key = self._key(scope, queue)
        with self._lock:
            pending = self._cache.get(key) or []
            pending.append(item.to_dict())
            self._cache.set(key, pending)

    def peek(self, scope: Scope, queue: str) -> tuple[Inbound, ...]:
        key = self._key(scope, queue)
        with self._lock:
            pending = self._cache.get(key) or []
        return tuple(Inbound.from_dict(raw) for raw in pending)

    def remove(self, scope: Scope, queue: str, item: Inbound) -> None:
        key = self._key(scope, queue)
        with self._lock:
            pending = self._cache.get(key) or []
            kept = [raw for raw in pending if Inbound.from_dict(raw).identity != item.identity]
            if len(kept) == len(pending):
                return
            if kept:
                self._cache.set(key, kept)
            else:
                self._cache.delete(key)

    def close(self) -> None:
        self._cache.close()


__all__ = ["DiskEventBuffer"]
