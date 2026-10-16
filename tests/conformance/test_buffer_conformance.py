"""`EventBuffer` 的契约：按队列名分桶、取走即出队、取空不报错。

buffer 只做一件事 —— **把已经离开队列、还没被 Brain 认领的输入留住**。它不判断重复、
不记录投递时间、不管 scope：那些要么归 journal，要么归一个 scope 一个进程这个前提。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from core.buffer import DiskEventBuffer
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import QueueName
from core.protocol.error import UnknownQueueError
from core.protocol.interface.buffer import EventBuffer


@pytest.fixture
def buffer(tmp_path: Path) -> Iterator[EventBuffer]:
    instance = DiskEventBuffer(tmp_path / "buffer")
    yield instance
    instance.close()


def message(index: int = 1, queue: str = QueueName.USER) -> Inbound:
    return Inbound(queue, "user.message", {"content": f"内容{index}"}, f"m{index}")


# --------------------------------------------------------------------------
# 分桶
# --------------------------------------------------------------------------


def test_add_stays_in_its_own_queue(buffer: EventBuffer) -> None:
    buffer.add(QueueName.USER, message(1))

    assert buffer.get_all(QueueName.USER) == (message(1),)
    assert buffer.get_all(QueueName.TOOL) == ()
    assert buffer.get_all(QueueName.SYSTEM) == ()


def test_the_three_queues_do_not_mix(buffer: EventBuffer) -> None:
    buffer.add(QueueName.USER, message(1))
    buffer.add(QueueName.TOOL, message(2))
    buffer.add(QueueName.SYSTEM, message(3))

    assert [item.source_id for item in buffer.get_all(QueueName.USER)] == ["m1"]
    assert [item.source_id for item in buffer.get_all(QueueName.TOOL)] == ["m2"]
    assert [item.source_id for item in buffer.get_all(QueueName.SYSTEM)] == ["m3"]


def test_unknown_queue_rejected(buffer: EventBuffer) -> None:
    with pytest.raises(UnknownQueueError):
        buffer.add("unknown", message())
    with pytest.raises(UnknownQueueError):
        buffer.get_all("unknown")


# --------------------------------------------------------------------------
# 取走
# --------------------------------------------------------------------------


def test_get_all_preserves_arrival_order(buffer: EventBuffer) -> None:
    for index in range(3):
        buffer.add(QueueName.USER, message(index))

    assert [item.source_id for item in buffer.get_all(QueueName.USER)] == ["m0", "m1", "m2"]


def test_get_all_on_an_empty_queue_returns_nothing(buffer: EventBuffer) -> None:
    assert buffer.get_all(QueueName.USER) == ()


def test_get_all_is_final(buffer: EventBuffer) -> None:
    """取走就是取走。认领失败要把输入放回去，放回去的人自己负责。"""
    buffer.add(QueueName.USER, message())
    buffer.get_all(QueueName.USER)

    assert buffer.get_all(QueueName.USER) == ()


def test_taking_one_queue_leaves_the_others(buffer: EventBuffer) -> None:
    buffer.add(QueueName.USER, message(1))
    buffer.add(QueueName.TOOL, message(2))
    buffer.get_all(QueueName.USER)

    assert buffer.get_all(QueueName.TOOL) == (message(2),)


# --------------------------------------------------------------------------
# 重复
# --------------------------------------------------------------------------


def test_the_same_item_added_twice_is_two_items(buffer: EventBuffer) -> None:
    """buffer 不做去重 —— 同一件事认领过两次由 journal 判定，不是这里。

    这里去重的话，认领失败放回来的输入会被后面的 `add` 吃掉，Brain 永远等不到它。
    """
    buffer.add(QueueName.USER, message())
    buffer.add(QueueName.USER, message())

    assert len(buffer.get_all(QueueName.USER)) == 2


# --------------------------------------------------------------------------
# 往返
# --------------------------------------------------------------------------


def test_items_survive_serialisation(buffer: EventBuffer) -> None:
    """落盘的代价是往返：读回来的必须是同一件事，不能掉字段。"""
    buffer.add(QueueName.USER, message(1))

    assert buffer.get_all(QueueName.USER) == (message(1),)
