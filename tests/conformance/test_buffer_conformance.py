"""`EventBuffer` 的契约：按 `(scope, 队列名)` 分桶、`peek` 不移出、`remove` 移出那一条。

buffer 只做一件事 —— **把已经离开队列、还没被 Brain 认领的输入留住**。它不判断重复、
不记录投递时间：那些要么归 journal，要么归认领那一侧。

**读和删必须分开。** 认领是「读出来 → 在 journal 里落下 → 从 buffer 删掉」三步；先删后写
的话崩在中间两头都没有，这条契约就是这两步分开的理由。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from core.buffer import DiskEventBuffer
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import QueueName
from core.protocol.datatype.scope import Scope
from core.protocol.error import UnknownQueueError
from core.protocol.interface.buffer import EventBuffer

ALICE = Scope("alice", "s1")
BOB = Scope("bob", "s1")


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
    buffer.add(ALICE, QueueName.USER, message(1))

    assert buffer.peek(ALICE, QueueName.USER) == (message(1),)
    assert buffer.peek(ALICE, QueueName.TOOL) == ()
    assert buffer.peek(ALICE, QueueName.SYSTEM) == ()


def test_the_three_queues_do_not_mix(buffer: EventBuffer) -> None:
    buffer.add(ALICE, QueueName.USER, message(1))
    buffer.add(ALICE, QueueName.TOOL, message(2))
    buffer.add(ALICE, QueueName.SYSTEM, message(3))

    assert [item.source_id for item in buffer.peek(ALICE, QueueName.USER)] == ["m1"]
    assert [item.source_id for item in buffer.peek(ALICE, QueueName.TOOL)] == ["m2"]
    assert [item.source_id for item in buffer.peek(ALICE, QueueName.SYSTEM)] == ["m3"]


def test_two_scopes_do_not_mix(buffer: EventBuffer) -> None:
    """**两个 scope 共用一个 buffer 实例。** 键里只有队列名的话，A 的一次认领会取走 B 的
    输入 —— A 的 journal 里会多出 B 说过的话，而 B 那条再也不会被认领。
    """
    buffer.add(ALICE, QueueName.USER, message(1))
    buffer.add(BOB, QueueName.USER, message(2))

    assert [item.source_id for item in buffer.peek(ALICE, QueueName.USER)] == ["m1"]
    assert [item.source_id for item in buffer.peek(BOB, QueueName.USER)] == ["m2"]

    buffer.remove(ALICE, QueueName.USER, message(1))

    assert buffer.peek(ALICE, QueueName.USER) == ()
    assert [item.source_id for item in buffer.peek(BOB, QueueName.USER)] == ["m2"], (
        "A 删自己的那一条，删掉了 B 的输入"
    )


def test_the_same_session_id_under_two_users_does_not_mix(buffer: EventBuffer) -> None:
    """session_id 一样不算同一个 scope —— 键是 `(user_id, session_id)` 整体。"""
    buffer.add(ALICE, QueueName.USER, message(1))
    buffer.add(Scope("bob", "s1"), QueueName.USER, message(2))

    assert len(buffer.peek(Scope("bob", "s1"), QueueName.USER)) == 1


def test_unknown_queue_rejected(buffer: EventBuffer) -> None:
    with pytest.raises(UnknownQueueError):
        buffer.add(ALICE, "unknown", message())
    with pytest.raises(UnknownQueueError):
        buffer.peek(ALICE, "unknown")


# --------------------------------------------------------------------------
# 读与删分开
# --------------------------------------------------------------------------


def test_peek_does_not_remove(buffer: EventBuffer) -> None:
    """认领要先把输入拿到别处落下，落下成功才删。这两步之间输入必须还在。"""
    buffer.add(ALICE, QueueName.USER, message())

    assert buffer.peek(ALICE, QueueName.USER) == (message(),)
    assert buffer.peek(ALICE, QueueName.USER) == (message(),)
    assert buffer.peek(ALICE, QueueName.USER) == (message(),)


def test_peek_preserves_arrival_order(buffer: EventBuffer) -> None:
    for index in range(3):
        buffer.add(ALICE, QueueName.USER, message(index))

    assert [item.source_id for item in buffer.peek(ALICE, QueueName.USER)] == ["m0", "m1", "m2"]


def test_peek_on_an_empty_queue_returns_nothing(buffer: EventBuffer) -> None:
    assert buffer.peek(ALICE, QueueName.USER) == ()


def test_remove_takes_out_only_that_one(buffer: EventBuffer) -> None:
    buffer.add(ALICE, QueueName.USER, message(1))
    buffer.add(ALICE, QueueName.USER, message(2))

    buffer.remove(ALICE, QueueName.USER, message(1))

    assert [item.source_id for item in buffer.peek(ALICE, QueueName.USER)] == ["m2"]


def test_removing_one_queue_leaves_the_others(buffer: EventBuffer) -> None:
    buffer.add(ALICE, QueueName.USER, message(1))
    buffer.add(ALICE, QueueName.TOOL, message(2))

    buffer.remove(ALICE, QueueName.USER, message(1))

    assert buffer.peek(ALICE, QueueName.USER) == ()
    assert buffer.peek(ALICE, QueueName.TOOL) == (message(2),)


def test_removing_something_absent_is_a_no_op(buffer: EventBuffer) -> None:
    """崩在「提交 journal 之后、删 buffer 之前」的话，下一轮认领会再删一次同一条。"""
    buffer.add(ALICE, QueueName.USER, message(1))

    buffer.remove(ALICE, QueueName.USER, message(1))
    buffer.remove(ALICE, QueueName.USER, message(1))

    assert buffer.peek(ALICE, QueueName.USER) == ()


def test_remove_survives_the_last_item_leaving(buffer: EventBuffer) -> None:
    """删到最后一条要把键整个去掉，不能留一个空列表。"""
    buffer.add(ALICE, QueueName.USER, message())
    buffer.remove(ALICE, QueueName.USER, message())

    assert buffer.peek(ALICE, QueueName.USER) == ()


# --------------------------------------------------------------------------
# 重复
# --------------------------------------------------------------------------


def test_the_same_item_added_twice_is_two_items(buffer: EventBuffer) -> None:
    """buffer 不做去重 —— 同一件事认领过两次由 journal 判定，不是这里。

    这里去重的话，重复投递的那条会被后面的 `add` 吃掉，Brain 永远等不到它。
    """
    buffer.add(ALICE, QueueName.USER, message())
    buffer.add(ALICE, QueueName.USER, message())

    assert len(buffer.peek(ALICE, QueueName.USER)) == 2


def test_remove_takes_out_every_copy_of_one_identity(buffer: EventBuffer) -> None:
    """两条一样的输入，认完之后 buffer 里不该还剩一条等下一轮再认一次。"""
    buffer.add(ALICE, QueueName.USER, message())
    buffer.add(ALICE, QueueName.USER, message())

    buffer.remove(ALICE, QueueName.USER, message())

    assert buffer.peek(ALICE, QueueName.USER) == ()


# --------------------------------------------------------------------------
# 往返
# --------------------------------------------------------------------------


def test_items_survive_serialisation(buffer: EventBuffer) -> None:
    """落盘的代价是往返：读回来的必须是同一件事，不能掉字段。"""
    buffer.add(ALICE, QueueName.USER, message(1))

    assert buffer.peek(ALICE, QueueName.USER) == (message(1),)
