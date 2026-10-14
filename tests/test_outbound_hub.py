"""`OutboundHub` 与 `WsEventSink` —— 帧怎么排进队列、什么时候被丢。

用一条假连接代替 socket：`send_text` / `close` 记下收到什么。**不开真 socket** ——
要测的是队列的界与背压，不是 TCP。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from core.outbound import ClientChannel, OutboundHub, WsEventSink
from core.protocol.datatype.event import AgentEvent
from core.protocol.datatype.message import MessageEvent, MessageEventType
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import CloseReason, FrameType
from core.protocol.interface.transport import SendResult
from core.utils.encoding import JsonObject

# --------------------------------------------------------------------------
# 替身与夹具
# --------------------------------------------------------------------------


class _Channel(ClientChannel):
    """假连接。写出去的东西留在 `sent` 里，close 记下理由。"""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed_with: str | None = None
        self.wrote = asyncio.Event()
        self.closed = asyncio.Event()
        self.fail_on_write = False

    async def send_text(self, text: str) -> None:
        if self.fail_on_write:
            raise ConnectionResetError("socket 断了")
        self.sent.append(text)
        self.wrote.set()

    async def close(self, reason: str) -> None:
        self.closed_with = reason
        self.closed.set()


@pytest.fixture
async def hub() -> AsyncIterator[OutboundHub]:
    yield OutboundHub(asyncio.get_running_loop(), send_queue_size=4)


def _frame(n: int) -> JsonObject:
    return {"type": "event", "n": n}


def _event(scope: Scope) -> AgentEvent:
    return MessageEvent(
        type=MessageEventType.ASSISTANT_MESSAGE,
        scope=scope,
        payload={"content": "晴"},
        idempotency_key="k",
        occur_dt="2026-10-13T00:00:00Z",
    )


async def _wait_for_writes(channel: _Channel, count: int) -> None:
    """等到写够 count 帧。一批里写几帧是不定的，不能按次数 wait 事件。"""
    for _ in range(1000):
        if len(channel.sent) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"只写出 {len(channel.sent)} 帧，要 {count}")


# --------------------------------------------------------------------------
# 排队
# --------------------------------------------------------------------------


async def test_a_frame_with_no_connection_is_dropped(hub: OutboundHub, scope: Scope) -> None:
    """没有别的地方可以补 —— 客户端没连上就是没连上。"""
    assert hub.send(scope, _frame(1)) is SendResult.NO_CONNECTION


async def test_an_attached_frame_is_accepted(hub: OutboundHub, scope: Scope) -> None:
    hub.attach(scope, _Channel())

    assert hub.send(scope, _frame(1)) is SendResult.ACCEPTED


async def test_the_queue_is_bounded(hub: OutboundHub, scope: Scope) -> None:
    """无界队列的话，一个不读的客户端能把整个 session 的内存吃光。"""
    hub.attach(scope, _Channel())
    results = [hub.send(scope, _frame(i)) for i in range(8)]

    assert results == [SendResult.ACCEPTED] * 4 + [SendResult.QUEUE_FULL] * 4


async def test_a_size_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="send_queue_size"):
        OutboundHub(asyncio.get_running_loop(), send_queue_size=0)


async def test_a_closed_connection_takes_no_more_frames(hub: OutboundHub, scope: Scope) -> None:
    hub.attach(scope, _Channel())
    hub.close(scope, CloseReason.IDLE_TIMEOUT)

    assert hub.send(scope, _frame(1)) is SendResult.NO_CONNECTION


async def test_each_scope_has_its_own_queue(hub: OutboundHub) -> None:
    one, other = Scope("u1", "s1"), Scope("u1", "s2")
    hub.attach(one, _Channel())

    assert hub.send(other, _frame(1)) is SendResult.NO_CONNECTION


# --------------------------------------------------------------------------
# 写出去
# --------------------------------------------------------------------------


async def test_the_pump_writes_what_was_queued(hub: OutboundHub, scope: Scope) -> None:
    channel = _Channel()
    hub.attach(scope, channel)

    hub.send(scope, _frame(1))
    await asyncio.wait_for(channel.wrote.wait(), timeout=1)

    assert len(channel.sent) == 1
    assert '"n":1' in channel.sent[0]


async def test_frames_go_out_in_order(hub: OutboundHub, scope: Scope) -> None:
    channel = _Channel()
    hub.attach(scope, channel)

    for i in range(3):
        hub.send(scope, _frame(i))
    await _wait_for_writes(channel, 3)

    assert [json.loads(frame)["n"] for frame in channel.sent] == [0, 1, 2]


async def test_a_dead_socket_drops_the_queue(hub: OutboundHub, scope: Scope) -> None:
    """socket 已经断了，队列里的帧是「交给传输了但没上网」的东西 —— 留着是假装还能补发。"""
    channel = _Channel()
    channel.fail_on_write = True
    hub.attach(scope, channel)

    hub.send(scope, _frame(1))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert hub.send(scope, _frame(2)) is SendResult.NO_CONNECTION


async def test_pumping_with_no_connection_returns(hub: OutboundHub, scope: Scope) -> None:
    """不是错：会话还没接上客户端而已。"""
    await hub.pump(scope)


async def test_detaching_stops_the_writer(hub: OutboundHub, scope: Scope) -> None:
    """写线程还活着，它等的那个 wake 再也不会有人来按了 —— 白占一条线程。"""
    channel = _Channel()
    hub.attach(scope, channel)
    task = hub._pumps[scope]

    hub.detach(scope, channel)
    await asyncio.sleep(0)

    assert task.cancelled()


# --------------------------------------------------------------------------
# 顶替
# --------------------------------------------------------------------------


async def test_a_new_connection_replaces_the_old_one(hub: OutboundHub, scope: Scope) -> None:
    first, second = _Channel(), _Channel()

    hub.attach(scope, first)
    hub.attach(scope, second)

    await asyncio.wait_for(first.closed.wait(), timeout=1)
    assert first.closed_with == CloseReason.SESSION_REPLACED


async def test_frames_go_to_the_replacement(hub: OutboundHub, scope: Scope) -> None:
    """顶替之后，旧连接那帧不会改道 —— 它的接收端已经是旧的了。"""
    hub.attach(scope, _Channel())
    replacement = _Channel()
    hub.attach(scope, replacement)

    assert hub.send(scope, _frame(1)) is SendResult.ACCEPTED
    assert hub.send(scope, _frame(2)) is SendResult.ACCEPTED


async def test_detach_does_not_clobber_the_replacement(hub: OutboundHub, scope: Scope) -> None:
    """前一条连接迟到地断开 —— 那时顶替者已经接管了，不能把它摘掉。"""
    first, second = _Channel(), _Channel()
    hub.attach(scope, first)
    hub.attach(scope, second)

    hub.detach(scope, first)

    assert hub.send(scope, _frame(1)) is SendResult.ACCEPTED


async def test_detach_removes_its_own_connection(hub: OutboundHub, scope: Scope) -> None:
    channel = _Channel()
    hub.attach(scope, channel)

    hub.detach(scope, channel)

    assert hub.send(scope, _frame(1)) is SendResult.NO_CONNECTION


# --------------------------------------------------------------------------
# WsEventSink
# --------------------------------------------------------------------------


async def test_an_event_becomes_an_event_frame(hub: OutboundHub, scope: Scope) -> None:
    channel = _Channel()
    hub.attach(scope, channel)

    WsEventSink(hub).emit(_event(scope))
    await asyncio.wait_for(channel.wrote.wait(), timeout=1)

    assert f'"{FrameType.EVENT}"' in channel.sent[0]
    assert f'"{MessageEventType.ASSISTANT_MESSAGE}"' in channel.sent[0]


async def test_a_full_queue_drops_the_connection(hub: OutboundHub, scope: Scope) -> None:
    """读得太慢的客户端，丢掉比无限缓冲好。"""
    channel = _Channel()
    hub.attach(scope, channel)
    sink = WsEventSink(hub)

    for _ in range(4):
        sink.emit(_event(scope))
    sink.emit(_event(scope))

    await asyncio.wait_for(channel.closed.wait(), timeout=1)
    assert channel.closed_with == CloseReason.SLOW_CONSUMER


async def test_an_event_with_no_connection_is_not_an_error(hub: OutboundHub, scope: Scope) -> None:
    """事件已经落盘，客户端没连上不是 core 的错 —— 抛出去只会让人以为对话丢了。"""
    WsEventSink(hub).emit(_event(scope))
