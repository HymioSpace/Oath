"""`DiskEventBuffer` —— 待认领输入的持久存放。

一条队列一个键，值是这一条队列当前全部输入的 JSON 列表。`get_all` 读完就把键删掉，
所以「取走」是一次读加一次删，写进 buffer 的输入不会在取走之后又出现。
"""

from __future__ import annotations

from pathlib import Path

from diskcache import Cache

from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import check_queue
from core.protocol.interface.buffer import EventBuffer


class DiskEventBuffer(EventBuffer):
    """本地文件里的待认领输入。进程崩了重启还在 —— 它是输入可靠性的来源。

    **一个进程一个 scope。** 所以键里只有队列名。
    """

    def __init__(self, directory: Path) -> None:
        self._cache = Cache(str(directory))

    def add(self, queue: str, item: Inbound) -> None:
        key = check_queue(queue)
        pending = self._cache.get(key) or []
        pending.append(item.to_dict())
        self._cache.set(key, pending)

    def get_all(self, queue: str) -> tuple[Inbound, ...]:
        key = check_queue(queue)
        pending = self._cache.pop(key, None)
        if not pending:
            return ()
        return tuple(Inbound.from_dict(raw) for raw in pending)

    def close(self) -> None:
        self._cache.close()


__all__ = ["DiskEventBuffer"]
