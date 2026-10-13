"""对外投递的 hub：帧排进哪个连接的队列，写出去。

**core 不 import `websockets`。** socket 的读写属于传输，core 只知道「有一个连接
能把帧写出去」——那件事由 `ClientChannel` 表达，具体实现（`bootstrap` 里的 WebSocket
适配器）去装。

有界发送队列在这里，也只能在这里：core 全是同步的、跑在 session 的工作线程上，
而写 socket 要在事件循环里。中间那座桥就是 `_pending`（线程安全的 deque，界由
`send_queue_size` 定）与一个无界的唤醒队列（只用来叫醒写线程，帧不经过它）。
"""

from __future__ import annotations

import asyncio
import threading
from abc import ABC, abstractmethod
from collections import deque

from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import CloseReason
from core.protocol.interface.transport import OutboundTransport, SendResult
from core.utils.encoding import JsonObject, canonical_json


class ClientChannel(ABC):
    """一条已连接的下行通道。**异步** —— 它跑在事件循环里。"""

    @abstractmethod
    async def send_text(self, text: str) -> None:
        """写一帧。已经断了要抛错，由这里关掉这条连接。"""

    @abstractmethod
    async def close(self, reason: str) -> None:
        """断开。`reason` 取 `CloseReason`，客户端据此退避。"""


class _Connection:
    """一个 scope 上的连接与它的有界发送队列。

    `pending` 用 deque 而不是 `asyncio.Queue`：判断「装不下了」必须同步发生在
    调用方那个线程里，等它进了循环再返回 `QUEUE_FULL` 就晚了。
    """

    __slots__ = ("channel", "closed", "lock", "pending", "size", "wake")

    def __init__(self, channel: ClientChannel, wake: asyncio.Queue[None], size: int) -> None:
        self.channel = channel
        self.pending: deque[JsonObject] = deque()
        self.lock = threading.Lock()
        self.wake = wake
        self.closed = False
        self.size = size


class OutboundHub(OutboundTransport):
    """按 scope 分流的发送队列。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, *, send_queue_size: int = 256) -> None:
        if send_queue_size < 1:
            raise ValueError(f"send_queue_size 必须是正整数，得到 {send_queue_size}")
        self._loop = loop
        self._size = send_queue_size
        self._conns: dict[Scope, _Connection] = {}
        self._pumps: dict[Scope, asyncio.Task[None]] = {}
        self._lock = threading.Lock()

    # -- 连接管理（传输层调，core 不调） -------------------------------------

    def attach(self, scope: Scope, channel: ClientChannel) -> None:
        """接上一条连接。同 scope 又有新的就把前一条顶掉。

        **必须在事件循环线程上调用** —— 它顺带把 `pump` 那个 task 起起来。
        """
        conn = _Connection(channel, asyncio.Queue(), self._size)
        with self._lock:
            previous = self._conns.get(scope)
            self._conns[scope] = conn
        if previous is not None:
            self._close(previous, CloseReason.SESSION_REPLACED)
        self._restart_pump(scope)

    def detach(self, scope: Scope, channel: ClientChannel) -> None:
        """断开后清掉。**只清自己那一条** —— 顶替者已经接管时不能把它摘掉。"""
        with self._lock:
            current = self._conns.get(scope)
            if current is not None and current.channel is channel:
                del self._conns[scope]
            else:
                return
        self._stop_pump(scope)

    # -- OutboundTransport -------------------------------------------------

    def send(self, scope: Scope, frame: JsonObject) -> SendResult:
        """把帧排进这条连接的队列。**不阻塞。**"""
        conn = self._lookup(scope)
        if conn is None:
            return SendResult.NO_CONNECTION
        with conn.lock:
            if conn.closed:
                return SendResult.NO_CONNECTION
            if len(conn.pending) >= conn.size:
                return SendResult.QUEUE_FULL
            conn.pending.append(frame)
        # 只叫醒写线程，不把帧塞进 wake —— 帧已经在 pending 里了。
        self._loop.call_soon_threadsafe(conn.wake.put_nowait, None)
        return SendResult.ACCEPTED

    def close(self, scope: Scope, reason: str, message: str = "") -> None:
        conn = self._lookup(scope)
        if conn is None:
            return
        self._close(conn, reason)

    def shutdown(self) -> None:
        """停掉所有写线程、忘掉所有连接。**必须在事件循环线程上调用。**

        **不发 close 帧** —— 没有一个 `CloseReason` 说的是「服务端要停了」，硬套一个
        会让客户端按错误的理由退避重连。socket 由传输层自己的 server 关。
        """
        with self._lock:
            conns = list(self._conns.values())
            self._conns.clear()
            scopes = list(self._pumps)
        for conn in conns:
            self._mark_closed(conn)
        for scope in scopes:
            self._stop_pump(scope)

    # -- 写线程 -------------------------------------------------------------

    async def pump(self, scope: Scope) -> None:
        """把这条连接的 pending 写出去。由传输层在自己的循环里起。

        写失败就地作废：socket 已经断了，队列里的帧是「已经交给传输了但还没上网」
        的东西，留着它们假装还能补发，不如老实作废让客户端重连。**不发 close 帧** ——
        能发的只有这条连接自己，而它已经坏了。
        """
        conn = self._lookup(scope)
        if conn is None:
            return
        try:
            while True:
                await conn.wake.get()
                batch = self._drain(conn)
                for frame in batch:
                    await conn.channel.send_text(canonical_json(frame))
        except asyncio.CancelledError:
            raise
        except Exception:
            self._mark_closed(conn)

    def _drain(self, conn: _Connection) -> list[JsonObject]:
        with conn.lock:
            batch = list(conn.pending)
            conn.pending.clear()
            return batch

    # -- 内部 ---------------------------------------------------------------

    def _restart_pump(self, scope: Scope) -> None:
        """换连接就要换写线程 —— 老的那个还挂在旧连接的 wake 上，永远等不到。"""
        self._stop_pump(scope)
        self._pumps[scope] = self._loop.create_task(self.pump(scope))

    def _stop_pump(self, scope: Scope) -> None:
        task = self._pumps.pop(scope, None)
        if task is not None:
            task.cancel()

    def _lookup(self, scope: Scope) -> _Connection | None:
        with self._lock:
            conn = self._conns.get(scope)
        if conn is None or conn.closed:
            return None
        return conn

    def _close(self, conn: _Connection, reason: str) -> None:
        if not self._mark_closed(conn):
            return
        self._loop.call_soon_threadsafe(self._start_close, conn, reason)

    def _start_close(self, conn: _Connection, reason: str) -> None:
        """在循环线程上起一个关连接的任务。

        `call_soon_threadsafe` 收的是**回调**，不是协程：把协程函数直接丢给它，
        返回的那个协程会被丢掉，谁也不会 await 它 —— 连接就永远关不掉。
        """
        self._loop.create_task(self._close_async(conn, reason))

    @staticmethod
    def _mark_closed(conn: _Connection) -> bool:
        with conn.lock:
            if conn.closed:
                return False
            conn.closed = True
            conn.pending.clear()
            return True

    async def _close_async(self, conn: _Connection, reason: str) -> None:
        try:
            await conn.channel.close(reason)
        except Exception:
            # 已经断了的连接关第二次会抛。断开本来就是目标，不用再报。
            return


__all__ = ["ClientChannel", "OutboundHub"]
