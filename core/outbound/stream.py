"""`OutboundStream` —— 把 journal 里的记录推给已连接的客户端。

一个 dispatcher 线程按订阅逐个拉 `outbound` feed，把每条记录编成 `EventFrame`
交给传输。**推不动就断开，不阻塞 core**：慢客户端拖住 dispatcher 的后果比断它
严重得多 —— 断开的连接能带着游标重连续上，阻塞的 dispatcher 会让所有 session
一起卡住。

游标语义与 `EventFeed` 完全一致（见 `core/feed/journal_feed.py`），所以「重连续上」
不需要额外机制：位置在 feed 里，断的是连接，不是位置。

**断时不 commit。** 已经交给传输但没送达的帧就是丢了，位置留在原地，客户端带着自己
收到的最后一个游标重连就能补上。这里 commit 了才会真的丢数据。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from core.feed.journal_feed import JournalEventFeed
from core.protocol.datatype.cursor import Cursor
from core.protocol.datatype.feed import FeedName
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import CloseReason, EventFrame
from core.protocol.interface.feed import EventFeed
from core.protocol.interface.journal import Journal
from core.protocol.interface.transport import OutboundTransport, SendResult


@dataclass(slots=True)
class Subscription:
    """一条活跃订阅。"""

    scope: Scope
    #: 累计送出的帧数。排查「客户端说没收到」时用。
    delivered: int = 0


class OutboundStream:
    """journal → 客户端的单向广播。

    v1 每个 scope 最多一条订阅：同一 session 只有一个活动 run，连接也不该有两个。
    重复 `subscribe` 顶掉前一条 —— **顶掉连接是调用方的责任**，传输那边要先关掉
    旧连接再订阅，否则会把新连接也关掉。
    """

    def __init__(
        self,
        journal: Journal,
        *,
        transport: OutboundTransport,
        feed: EventFeed | None = None,
        batch: int = 256,
        poll_interval: float = 1.0,
    ) -> None:
        self._feed = feed if feed is not None else JournalEventFeed(journal)
        self._transport = transport
        self._batch = batch
        self._poll_interval = poll_interval
        self._lock = threading.Condition()
        self._subs: dict[tuple[str, str], Subscription] = {}
        self._closed = False
        self._thread: threading.Thread | None = None

    # -- 订阅 --------------------------------------------------------------

    def subscribe(self, scope: Scope, *, resume_after: Cursor | None = None) -> None:
        """开始给这个 scope 推事件。

        位置**以客户端给的游标为准**：`resume_after` 之后才推。没给就从日志开头
        推 —— 对外流收全部记录，客户端要看到 agent 干了什么，跳不跳过是它自己的事
        （客户端拿不准就带游标来）。
        """
        self._feed.reset(scope, FeedName.OUTBOUND)
        if resume_after is not None:
            self._feed.commit(scope, FeedName.OUTBOUND, resume_after)
        with self._lock:
            self._subs[(scope.user_id, scope.session_id)] = Subscription(scope=scope)
            self._ensure_running_locked()
            self._lock.notify_all()

    def unsubscribe(self, scope: Scope) -> None:
        """停止推送。不动位置 —— 位置还可能被打断的订阅用得上。"""
        with self._lock:
            self._subs.pop((scope.user_id, scope.session_id), None)

    def subscriptions(self) -> tuple[Subscription, ...]:
        with self._lock:
            return tuple(self._subs.values())

    # -- 生命周期 ----------------------------------------------------------

    def wake(self, scope: Scope) -> None:
        """有新记录落进这个 scope 了。与 `EventFeed.wake` 同名同义。"""
        with self._lock:
            self._lock.notify_all()

    def close(self, *, wait: bool = True) -> None:
        """停 dispatcher。已经交给传输的帧不受影响。"""
        with self._lock:
            self._closed = True
            self._lock.notify_all()
            thread = self._thread
        if wait and thread is not None:
            thread.join()

    # -- dispatcher --------------------------------------------------------

    def _ensure_running_locked(self) -> None:
        if self._closed or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="oath-outbound", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._closed:
                    return
                subs = tuple(self._subs.values())
                if not subs:
                    self._lock.wait(self._poll_interval)
                    continue
            for sub in subs:
                self._drain(sub)

    def _drain(self, sub: Subscription) -> bool:
        """把这个 scope 的 outbound feed 推空。返回 False 表示订阅已退掉。"""
        records = self._feed.pull(sub.scope, FeedName.OUTBOUND, limit=self._batch)
        if not records:
            return True
        for record in records:
            frame = EventFrame.of(sub.scope, record)
            result = self._transport.send(sub.scope, frame.to_json())
            if result is SendResult.ACCEPTED:
                sub.delivered += 1
                continue
            if result is SendResult.QUEUE_FULL:
                self._transport.close(sub.scope, CloseReason.SLOW_CONSUMER)
            # 故意不 commit：这些帧没送达，位置留在原地等客户端带游标回来补。
            # 两种情况都不再推：连接没了，订阅留着只是白读一遍 journal。
            self.unsubscribe(sub.scope)
            return False
        self._feed.commit(sub.scope, FeedName.OUTBOUND, records[-1].cursor)
        return True


__all__ = ["OutboundStream", "Subscription"]
