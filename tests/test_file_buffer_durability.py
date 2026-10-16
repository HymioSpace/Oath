"""Buffer 落盘的意义只有一个：**进程崩了，已经从队列取走但还没认领的输入还在。**

所以这里只测一件事 —— 关掉再打开，队列分桶和内容都还在。取走之后不该留下东西，那是
`conformance/test_buffer_conformance.py` 的事。
"""

from contextlib import closing
from pathlib import Path

from core.buffer import DiskEventBuffer
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import QueueName


def message(index: int = 1, queue: str = QueueName.USER) -> Inbound:
    return Inbound(queue, "user.message", {"content": f"内容{index}"}, f"m{index}")


def test_input_survives_a_restart(tmp_path: Path) -> None:
    with closing(DiskEventBuffer(tmp_path / "buffer")) as before:
        before.add(QueueName.USER, message(1))

    with closing(DiskEventBuffer(tmp_path / "buffer")) as after:
        assert after.get_all(QueueName.USER) == (message(1),)


def test_every_queue_survives_its_own_restart(tmp_path: Path) -> None:
    """三条队列是三个键 —— 只重开一个 scope 时不能把别的桶一起带走。"""
    with closing(DiskEventBuffer(tmp_path / "buffer")) as before:
        before.add(QueueName.USER, message(1))
        before.add(QueueName.TOOL, message(2))
        before.add(QueueName.SYSTEM, message(3))

    with closing(DiskEventBuffer(tmp_path / "buffer")) as after:
        assert after.get_all(QueueName.USER) == (message(1),)
        assert after.get_all(QueueName.TOOL) == (message(2),)
        assert after.get_all(QueueName.SYSTEM) == (message(3),)


def test_taken_input_does_not_come_back(tmp_path: Path) -> None:
    """认领完的输入已经落进 journal，重启后再冒出来就是同一条事实算两遍。"""
    with closing(DiskEventBuffer(tmp_path / "buffer")) as before:
        before.add(QueueName.USER, message())

    with closing(DiskEventBuffer(tmp_path / "buffer")) as middle:
        assert len(middle.get_all(QueueName.USER)) == 1

    with closing(DiskEventBuffer(tmp_path / "buffer")) as after:
        assert after.get_all(QueueName.USER) == ()


def test_a_partial_take_keeps_what_was_not_taken(tmp_path: Path) -> None:
    """认领中途失败要放回去的是一批输入，不是把整个桶清空。"""
    with closing(DiskEventBuffer(tmp_path / "buffer")) as before:
        for index in range(3):
            before.add(QueueName.USER, message(index))
        before.get_all(QueueName.USER)
        before.add(QueueName.USER, message(9))

    with closing(DiskEventBuffer(tmp_path / "buffer")) as after:
        assert [item.source_id for item in after.get_all(QueueName.USER)] == ["m9"]
