"""`RedisEventQueue` —— Redis Streams 上的三条队列。

key 按 `(user_id, session_id, 队列名)` 分开，每个 key 一个消费组。**一个 scope 一个 agent
进程**，所以每个组里只有一个 consumer；进程重启换 consumer 名字，靠 `XAUTOCLAIM` 把上一
个进程没取完的 pending 收回来。

消费组的语义落在实现里，不在 `EventQueue` 接口里。取出来的消息先不 ack，等下一次取之前
再 ack：调用方把这一批写进 buffer 是同步的，到下一次 `get_all` 那一刻它一定写完了。崩在
写 buffer 之前，消息还在 pending 里，下次启动被 `XAUTOCLAIM` 收回；崩在写完之后，重投也
只会重复一次 —— 认领时按 `(kind, source_id)` 去重。
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from typing import Any

import redis

from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import check_queue
from core.protocol.datatype.scope import Scope
from core.protocol.interface.queue import EventQueue
from core.utils.encoding import canonical_json

#: 一轮最多取多少条。取完立即 ack 并删除，所以 stream 不会长。
_BATCH = 128
#: `BLOCK` 的上限。**必须短于消费线程的 join 上限** —— 停机时 `close()` 叫不醒一个
#: 阻塞在 `XREADGROUP` 上的线程，只能等它自己超时回来。
_MAX_BLOCK_MS = 1_000
_GROUP = "oath"


class RedisEventQueue(EventQueue):
    def __init__(self, client: redis.Redis) -> None:
        self._redis = client
        self._consumer = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._joined: set[str] = set()
        #: 已经交给调用方、还没 ack 的消息 id。取到的下一批不重复它们。
        self._unacked: dict[str, list[Any]] = {}
        self._closed = False

    @staticmethod
    def _key(scope: Scope, queue: str) -> str:
        return f"oath:queue:{scope.user_id}:{scope.session_id}:{queue}"

    def _join(self, key: str) -> None:
        if key in self._joined:
            return
        try:
            self._redis.xgroup_create(key, _GROUP, id="0", mkstream=True)
        except redis.ResponseError as exc:
            # BUSYGROUP：这一组已经在了。前一个进程建过，重启后共用同一组。
            if "BUSYGROUP" not in str(exc):
                raise
        self._joined.add(key)

    def add(self, scope: Scope, item: Inbound) -> None:
        queue = check_queue(item.kind)
        self._redis.xadd(self._key(scope, queue), {"item": canonical_json(item.to_dict())})

    def get_all(self, scope: Scope, queue: str) -> tuple[Inbound, ...]:
        check_queue(queue)
        key = self._key(scope, queue)
        self._join(key)
        self._confirm(key)
        claimed = set(self._unacked.pop(key, ()))
        # 上一个进程没取完的先收回来，再取本 consumer 名下的 pending。
        reclaimed = self._redis.xautoclaim(key, _GROUP, self._consumer, 0, 0, count=_BATCH)
        pending = self._redis.xreadgroup(_GROUP, self._consumer, count=_BATCH, streams={key: "0"})
        # XAUTOCLAIM 会把本 consumer 自己名下的 pending 也一起收回来，所以两次读到的
        # 可能有重叠 —— 按消息 id 去重，重复进 buffer 就成了两条一样的输入。
        entries: dict[Any, dict[bytes, bytes]] = {}
        for entry in (*_entries(reclaimed), *_entries(pending)):
            if entry[0] not in claimed:
                entries.setdefault(entry[0], entry[1])
        if not entries:
            return ()
        # 不在这里 ack：调用方还没把它们写进 buffer。下一轮取之前再确认。
        self._unacked[key] = list(entries)
        return tuple(Inbound.from_dict(json.loads(fields[b"item"])) for fields in entries.values())

    def _confirm(self, key: str) -> None:
        """确认上一批已交给调用方的消息，并从 stream 里删掉。"""
        ids = self._unacked.get(key)
        if not ids:
            return
        self._redis.xack(key, _GROUP, *ids)
        self._redis.xdel(key, *ids)

    def listen(self, scope: Scope, queue: str, timeout: float) -> bool:
        """阻塞等新消息。返回的消息进 pending，下一次 `get_all` 取走。"""
        check_queue(queue)
        key = self._key(scope, queue)
        self._join(key)
        if self._closed:
            return False
        block = max(1, min(int(timeout * 1000), _MAX_BLOCK_MS))
        messages = self._redis.xreadgroup(
            _GROUP,
            self._consumer,
            count=1,
            block=block,
            streams={key: ">"},
        )
        return bool(messages)

    def close(self, *, wait: bool = True) -> None:
        """停机。已经取走但还没写进 buffer 的输入留在 pending 里，下一个进程收得回来。"""
        self._closed = True


def _entries(response: Any) -> list[tuple[Any, dict[bytes, bytes]]]:
    """`XREADGROUP` / `XAUTOCLAIM` 的回复转成 `[消息 id, 字段]` 列表。

    两种回复的外层形状不同：`XREADGROUP` 是 `[[stream, entries], ...]`，
    `XAUTOCLAIM` 是 `[next_id, entries, deleted]` —— 后者的 `entries` 已经是消息列表，
    没有 stream 那一层。按第一项是字符串还是列表来区分，只展平前一种。

    消息 id 原样保留：它是 stream id，`XACK` / `XDEL` 要的就是这个字节串。
    """
    if not response:
        return []
    if isinstance(response[0], (str, bytes)):
        return [(item[0], item[1]) for item in response[1]]
    return [item for _, entries in response for item in entries]


__all__ = ["RedisEventQueue"]
