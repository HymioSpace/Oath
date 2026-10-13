"""`OutboundStream` —— 把 journal 里的记录推给已连接的客户端。

一个 dispatcher 线程按订阅读 journal，只推 `CLIENT_FACING` 的记录。

**它不是队列。** oath 内部没有消费方：没有「处理完才推进」这回事，缓冲与重排是
客户端自己的事。所以这里记的读位置不是记账 —— 断线后位置由客户端给的
`resume_after` 重置，oath 自己读到哪儿只影响「别每次 poll 都重扫整份日志」。

**推不动就断开，不阻塞 core**：慢客户端拖住 dispatcher 的后果比断它严重得多 ——
断开的连接能带着游标重连续上，阻塞的 dispatcher 会让所有 session 一起卡住。

**位置不是记账。** 读到哪里就记到哪里，包括没能送出去的那几帧 —— 少了的那段靠
客户端自己带的 `resume_after` 补，oath 这边的位置只管「别每次 poll 都重扫整份日志」。
所以 `CLIENT_FACING` 之外的记录照样推进，否则一批全是 `user.message` 的行会让
poll 空转到把日志读完为止。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from core.journal.cursor import beginning, check_kind
from core.protocol.datatype.cursor import Cursor
from core.protocol.datatype.feed import CLIENT_FACING
from core.protocol.datatype.journal import Record
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import CloseReason, EventFrame
from core.protocol.interface.journal import Journal
from core.protocol.interface.transport import OutboundTransport, SendResult


@dataclass(slots=True)
class Subscription:
    """一条活跃订阅。"""

    scope: Scope
    #: 读到哪儿了。不是消费位置 —— 见模块文档。
    cursor: Cursor
    #: 读到了但还没送出的记录。
    pending: list[Record] = field(default_factory=list)
    #: 累计送出的帧数。排查「客户端说没收到」时用。
    delivered: int = 0


class OutboundStream:
    """journal → 客户端的单向推送。

    v1 每个 scope 最多一条订阅：同一 session 只有一个活动 run，连接也不该有两个。
    重复 `subscribe` 顶掉前一条 —— **顶掉连接是调用方的责任**，传输那边要先关掉
    旧连接再订阅，否则会把新连接也关掉。
    """

    def __init__(
        self,
        journal: Journal,
        *,
        transport: OutboundTransport,
        batch: int = 256,
        poll_interval: float = 1.0,
    ) -> None:
        self._journal = journal
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

        位置**以客户端给的游标为准**：`resume_after` 之后才推。没给就从日志开头推 ——
        客户端要看到 agent 干了什么，跳不跳过是它自己的事（拿不准就带游标来）。
        """
        start = beginning(self._journal.cursor_kind)
        if resume_after is not None:
            check_kind(resume_after, frozenset({self._journal.cursor_kind}))
            start = resume_after
        with self._lock:
            self._subs[(scope.user_id, scope.session_id)] = Subscription(scope=scope, cursor=start)
            self._ensure_running_locked()
            self._lock.notify_all()

    def unsubscribe(self, scope: Scope) -> None:
        """停止推送。位置随订阅一起消失 —— 客户端重连时会带新的 `resume_after`。"""
        with self._lock:
            self._subs.pop((scope.user_id, scope.session_id), None)

    def subscriptions(self) -> tuple[Subscription, ...]:
        with self._lock:
            return tuple(self._subs.values())

    # -- 生命周期 ----------------------------------------------------------

    def wake(self) -> None:
        """有新记录落进来了。"""
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
        """把这个 scope 该推的记录推一批。返回 False 表示订阅已退掉。"""
        if not sub.pending:
            scanned = False
            for record in self._journal.read_range(sub.scope, sub.cursor, limit=self._batch):
                scanned = True
                if record.type in CLIENT_FACING:
                    sub.pending.append(record)
                # 不属于对外流的照样推进，否则空转也能把整份日志读完。
                sub.cursor = record.cursor
            if not scanned:
                return True
        while sub.pending:
            record = sub.pending.pop(0)
            result = self._transport.send(sub.scope, EventFrame.of(sub.scope, record).to_json())
            if result is SendResult.ACCEPTED:
                sub.delivered += 1
                continue
            if result is SendResult.QUEUE_FULL:
                self._transport.close(sub.scope, CloseReason.SLOW_CONSUMER)
            # 两种情况都不再推：连接没了，订阅留着只是白读一遍 journal。
            self.unsubscribe(sub.scope)
            return False
        return True


__all__ = ["OutboundStream", "Subscription"]
