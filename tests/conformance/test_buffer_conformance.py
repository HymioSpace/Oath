"""`EventBuffer` 契约测试。

这些测试跑在 `EventBuffer` **接口**上，只描述接口承诺的语义（`snapshot` 不删除、
`clear` 之后才算完、scope 之间查不到、`scopes` 只报没清完的），不碰实现细节
（文件路径、fd、append 的写法）。

将来换实现（SQLite），同一组测试应该原样跑通 —— 那时把它们参数化到多个实现上，
而不是复制一份。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from core.buffer import FileEventBuffer
from core.protocol.datatype import Inbound, InboundKind, Scope
from core.protocol.error import UnknownQueueError
from core.protocol.interface import EventBuffer

SCOPE = Scope(user_id="u1", session_id="s1")
OTHER_SCOPE = Scope(user_id="u1", session_id="s2")
OTHER_USER = Scope(user_id="u2", session_id="s1")


@pytest.fixture
def buffer(tmp_path: Path) -> Iterator[EventBuffer]:
    """v1 只有文件一种实现。加第二个实现时把这个 fixture 参数化，不要复制测试。"""
    yield FileEventBuffer(tmp_path)


def item(index: int, *, kind: str = InboundKind.USER) -> Inbound:
    return Inbound(
        kind=kind,
        source=f"src-{index}",
        payload={"content": f"第 {index} 条"},
        source_id=f"id-{index}",
        occur_dt="2026-01-01T00:00:00.000000Z",
    )


def ids(items: tuple[Inbound, ...]) -> list[str]:
    return [i.source_id for i in items]


# --------------------------------------------------------------------------
# 写与读
# --------------------------------------------------------------------------


def test_snapshot_on_an_untouched_queue_is_empty(buffer: EventBuffer) -> None:
    assert buffer.snapshot(SCOPE, "user") == ()


def test_put_is_visible_to_snapshot(buffer: EventBuffer) -> None:
    """搬进 buffer 那一刻起就不再会丢 —— 这是整条链上唯一的耐久性承诺。"""
    buffer.put(SCOPE, "user", (item(1),))

    assert ids(buffer.snapshot(SCOPE, "user")) == ["id-1"]


def test_put_appends_in_order(buffer: EventBuffer) -> None:
    buffer.put(SCOPE, "tool", (item(1), item(2)))
    buffer.put(SCOPE, "tool", (item(3),))

    assert ids(buffer.snapshot(SCOPE, "tool")) == ["id-1", "id-2", "id-3"]


def test_put_of_nothing_does_nothing(buffer: EventBuffer) -> None:
    buffer.put(SCOPE, "user", ())

    assert buffer.snapshot(SCOPE, "user") == ()


def test_every_field_survives_a_round_trip(buffer: EventBuffer) -> None:
    original = item(1)
    buffer.put(SCOPE, "user", (original,))

    assert buffer.snapshot(SCOPE, "user")[0] == original


def test_the_three_queues_are_separate(buffer: EventBuffer) -> None:
    buffer.put(SCOPE, "user", (item(1),))
    buffer.put(SCOPE, "tool", (item(2),))

    assert ids(buffer.snapshot(SCOPE, "user")) == ["id-1"]
    assert ids(buffer.snapshot(SCOPE, "tool")) == ["id-2"]


def test_queues_do_not_leak_across_scopes(buffer: EventBuffer) -> None:
    buffer.put(SCOPE, "user", (item(1),))
    buffer.put(OTHER_SCOPE, "user", (item(2),))
    buffer.put(OTHER_USER, "user", (item(3),))

    assert ids(buffer.snapshot(SCOPE, "user")) == ["id-1"]
    assert ids(buffer.snapshot(OTHER_SCOPE, "user")) == ["id-2"]
    assert ids(buffer.snapshot(OTHER_USER, "user")) == ["id-3"]


def test_unknown_queue_raises(buffer: EventBuffer) -> None:
    with pytest.raises(UnknownQueueError):
        buffer.put(SCOPE, "nope", (item(1),))


# --------------------------------------------------------------------------
# snapshot 不删除
# --------------------------------------------------------------------------


def test_snapshot_does_not_consume(buffer: EventBuffer) -> None:
    """**核心契约。** brain 要在处理期间反复读同一批 —— 读一次就没了的话，
    「处理完才清」根本无从谈起。"""
    buffer.put(SCOPE, "tool", (item(1),))

    assert ids(buffer.snapshot(SCOPE, "tool")) == ["id-1"]
    assert ids(buffer.snapshot(SCOPE, "tool")) == ["id-1"]
    assert ids(buffer.snapshot(SCOPE, "tool")) == ["id-1"]


# --------------------------------------------------------------------------
# clear
# --------------------------------------------------------------------------


def test_clear_empties_the_queue(buffer: EventBuffer) -> None:
    buffer.put(SCOPE, "user", (item(1),))
    buffer.clear(SCOPE, "user")

    assert buffer.snapshot(SCOPE, "user") == ()


def test_clear_leaves_the_other_queues_alone(buffer: EventBuffer) -> None:
    buffer.put(SCOPE, "user", (item(1),))
    buffer.put(SCOPE, "tool", (item(2),))
    buffer.clear(SCOPE, "user")

    assert ids(buffer.snapshot(SCOPE, "tool")) == ["id-2"]


def test_clear_on_an_untouched_queue_is_not_an_error(buffer: EventBuffer) -> None:
    buffer.clear(SCOPE, "user")

    assert buffer.snapshot(SCOPE, "user") == ()


def test_put_after_clear_appends_to_a_fresh_queue(buffer: EventBuffer) -> None:
    buffer.put(SCOPE, "user", (item(1),))
    buffer.clear(SCOPE, "user")
    buffer.put(SCOPE, "user", (item(2),))

    assert ids(buffer.snapshot(SCOPE, "user")) == ["id-2"]


# --------------------------------------------------------------------------
# scopes
# --------------------------------------------------------------------------


def test_scopes_is_empty_when_nothing_is_pending(buffer: EventBuffer) -> None:
    assert buffer.scopes() == ()


def test_scopes_reports_what_has_not_been_cleared(buffer: EventBuffer) -> None:
    """重启恢复的入口 —— 起来时靠它把没干完的活捞回来。"""
    buffer.put(SCOPE, "user", (item(1),))
    buffer.put(OTHER_USER, "tool", (item(2),))

    assert set(buffer.scopes()) == {SCOPE, OTHER_USER}


def test_scopes_forgets_a_cleared_scope(buffer: EventBuffer) -> None:
    buffer.put(SCOPE, "user", (item(1),))
    buffer.clear(SCOPE, "user")

    assert buffer.scopes() == ()


def test_scopes_reports_a_scope_whose_batch_is_still_growing(buffer: EventBuffer) -> None:
    buffer.put(SCOPE, "tool", (item(1),))
    buffer.put(SCOPE, "tool", (item(2),))

    assert set(buffer.scopes()) == {SCOPE}
