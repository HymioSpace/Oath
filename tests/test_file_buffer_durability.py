"""Buffer 落盘的意义只有一个：**进程崩了，已经从队列取走但还没认领的输入还在。**

所以这里测三件事：关掉再打开，输入还在；不同 scope 与不同队列各自分开；只有 `remove`
掉的那条不再回来。读不移出是 `conformance/test_buffer_conformance.py` 的事。
"""

from contextlib import closing
from pathlib import Path

from core.buffer import DiskEventBuffer
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import QueueName
from core.protocol.datatype.scope import Scope

ALICE = Scope("alice", "s1")
BOB = Scope("bob", "s1")


def message(index: int = 1, queue: str = QueueName.USER) -> Inbound:
    return Inbound(queue, "user.message", {"content": f"内容{index}"}, f"m{index}")


def test_input_survives_a_restart(tmp_path: Path) -> None:
    with closing(DiskEventBuffer(tmp_path / "buffer")) as before:
        before.add(ALICE, QueueName.USER, message(1))

    with closing(DiskEventBuffer(tmp_path / "buffer")) as after:
        assert after.peek(ALICE, QueueName.USER) == (message(1),)


def test_every_queue_survives_its_own_restart(tmp_path: Path) -> None:
    """三条队列是三个键 —— 只重开一个 scope 时不能把别的桶一起带走。"""
    with closing(DiskEventBuffer(tmp_path / "buffer")) as before:
        before.add(ALICE, QueueName.USER, message(1))
        before.add(ALICE, QueueName.TOOL, message(2))
        before.add(ALICE, QueueName.SYSTEM, message(3))

    with closing(DiskEventBuffer(tmp_path / "buffer")) as after:
        assert after.peek(ALICE, QueueName.USER) == (message(1),)
        assert after.peek(ALICE, QueueName.TOOL) == (message(2),)
        assert after.peek(ALICE, QueueName.SYSTEM) == (message(3),)


def test_two_scopes_share_one_directory_but_not_one_bucket(tmp_path: Path) -> None:
    """同一个 `data_root/buffer` 上跑着所有 agent 进程。**键里必须有 scope** —— 只有队列名
    的话，A 的一次认领会连 B 的输入一起取走，那条再也认不到了。
    """
    with closing(DiskEventBuffer(tmp_path / "buffer")) as before:
        before.add(ALICE, QueueName.USER, message(1))
        before.add(BOB, QueueName.USER, message(2))

    with closing(DiskEventBuffer(tmp_path / "buffer")) as after:
        assert [item.source_id for item in after.peek(ALICE, QueueName.USER)] == ["m1"]
        assert [item.source_id for item in after.peek(BOB, QueueName.USER)] == ["m2"]

        after.remove(ALICE, QueueName.USER, message(1))

    with closing(DiskEventBuffer(tmp_path / "buffer")) as last:
        assert last.peek(ALICE, QueueName.USER) == ()
        assert [item.source_id for item in last.peek(BOB, QueueName.USER)] == ["m2"]


def test_removed_input_does_not_come_back(tmp_path: Path) -> None:
    """认领完的输入已经落进 journal，重启后再冒出来就是同一条事实算两遍。"""
    with closing(DiskEventBuffer(tmp_path / "buffer")) as before:
        before.add(ALICE, QueueName.USER, message())

    with closing(DiskEventBuffer(tmp_path / "buffer")) as middle:
        assert len(middle.peek(ALICE, QueueName.USER)) == 1
        middle.remove(ALICE, QueueName.USER, message())

    with closing(DiskEventBuffer(tmp_path / "buffer")) as after:
        assert after.peek(ALICE, QueueName.USER) == ()


def test_a_crash_before_removing_keeps_the_input(tmp_path: Path) -> None:
    """认领分三步：读、写 journal、删。**崩在写之后、删之前**，输入要还在 —— journal 那边
    按身份去重认掉它，下一轮的 `_discard_claimed` 再把它收走。删早了这条就两头都没有。
    """
    with closing(DiskEventBuffer(tmp_path / "buffer")) as before:
        for index in range(3):
            before.add(ALICE, QueueName.USER, message(index))

    with closing(DiskEventBuffer(tmp_path / "buffer")) as crashed:
        # 认领走了第 1 条，写进了 journal，还没来得及删就崩了。
        assert len(crashed.peek(ALICE, QueueName.USER)) == 3

    with closing(DiskEventBuffer(tmp_path / "buffer")) as after:
        assert [item.source_id for item in after.peek(ALICE, QueueName.USER)] == ["m0", "m1", "m2"]
        after.remove(ALICE, QueueName.USER, message(1))
        after.remove(ALICE, QueueName.USER, message(0))

    with closing(DiskEventBuffer(tmp_path / "buffer")) as last:
        assert [item.source_id for item in last.peek(ALICE, QueueName.USER)] == ["m2"]
