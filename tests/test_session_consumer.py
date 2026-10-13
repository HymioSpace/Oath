"""`SessionConsumer` —— 队列 → buffer → brain → clear。

关键是把「搬运」和「判断」分开：这一层一次问一次、搬一次，该不该醒、该不该清 buffer
全是 `Brain` 说了算。这里用一个假的 `Brain` 把它当替身验。

`FakeBrain` 把「该不该处理」做成可配置的返回值，把「等多久」做成可配置的秒数，
并记下每次交上来的是什么。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from core.buffer import FileEventBuffer
from core.consumer import SessionConsumer
from core.protocol.datatype import Batch, Inbound, InboundKind, Scope
from core.protocol.interface import Brain, Journal
from core.queue import InMemoryEventQueue
from tests._support.agent_fixtures import agent_ctx

# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------


class FakeBrain(Brain):
    """`settled` 决定 `step` 返回什么，`delay` 决定之后等多久。"""

    def __init__(self, *, settled: bool = True, delay: float | None = None) -> None:
        self.settled = settled
        self.delay = delay
        self.batches: list[Batch] = []
        self.delays_asked: int = 0
        self.closed = False

    def step(self, scope: Scope, batch: Batch) -> bool:
        self.batches.append(batch)
        return self.settled

    def wakeup_delay(self, scope: Scope, batch: Batch) -> float | None:
        self.delays_asked += 1
        return self.delay

    def close(self, *, wait: bool = True) -> None:
        self.closed = True


class _RecordingQueue(InMemoryEventQueue):
    """记下每次 `wait` 的 timeout，替掉真睡。"""

    def __init__(self) -> None:
        super().__init__()
        self.timeouts: list[float] = []

    def wait(self, scope: Scope, *, timeout: float | None = None) -> bool:
        self.timeouts.append(timeout or 0.0)
        return False


def _user(index: int) -> Inbound:
    return Inbound(
        kind=InboundKind.USER,
        source="user.message",
        payload={"content": f"第 {index} 条"},
        source_id=f"id-{index}",
        occur_dt=f"t{index}",
    )


def _tool(index: int) -> Inbound:
    return Inbound(
        kind=InboundKind.TOOL_RESULT,
        source="tool.terminal",
        payload={"invocation_id": f"id-{index}", "outcome": "succeeded", "content": "ok"},
        source_id=f"id-{index}",
        occur_dt=f"t{index}",
    )


def _invoke(index: int) -> Inbound:
    return Inbound(kind=InboundKind.INVOKE, source="run.cancel", source_id=f"id-{index}")


def _ids(items: tuple[Inbound, ...]) -> list[str]:
    return [item.source_id for item in items]


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture
def queue() -> _RecordingQueue:
    return _RecordingQueue()


@pytest.fixture
def brain() -> FakeBrain:
    return FakeBrain()


@pytest.fixture
def buffer(tmp_path: Path) -> FileEventBuffer:
    return FileEventBuffer(tmp_path / "buffers")


@pytest.fixture
def consumer(
    journal: Journal,
    queue: _RecordingQueue,
    brain: FakeBrain,
    buffer: FileEventBuffer,
) -> tuple[SessionConsumer, _RecordingQueue, FakeBrain, FileEventBuffer]:
    return SessionConsumer(agent_ctx(journal, queue=queue), brain, buffer), queue, brain, buffer


# --------------------------------------------------------------------------
# 队列 → buffer
# --------------------------------------------------------------------------


def test_an_emptied_queue_lands_in_the_buffer(
    consumer: tuple[SessionConsumer, _RecordingQueue, FakeBrain, FileEventBuffer], scope: Scope
) -> None:
    # brain 说不算，buffer 才留得住 —— 否则这一轮就被清空了，看不到搬进来的东西。
    session, queue, _, buffer = consumer
    session._brain = FakeBrain(settled=False)
    queue.put(scope, _user(1))

    session._cycle(scope)

    # 队列里没了，buffer 里有 —— 从这一刻起不会再丢。
    assert queue.drain(scope, "user", limit=64) == ()
    assert buffer.scopes() == (scope,)


def test_each_queue_lands_in_its_own_buffer(
    consumer: tuple[SessionConsumer, _RecordingQueue, FakeBrain, FileEventBuffer], scope: Scope
) -> None:
    session, queue, _, buffer = consumer
    session._brain = FakeBrain(settled=False)
    queue.put(scope, _user(1))
    queue.put(scope, _tool(2))
    queue.put(scope, _invoke(3))

    session._cycle(scope)

    assert _ids(buffer.snapshot(scope, "user")) == ["id-1"]
    assert _ids(buffer.snapshot(scope, "tool")) == ["id-2"]
    assert _ids(buffer.snapshot(scope, "system")) == ["id-3"]


def test_an_empty_queue_writes_nothing(
    consumer: tuple[SessionConsumer, _RecordingQueue, FakeBrain, FileEventBuffer], scope: Scope
) -> None:
    _, _, brain, buffer = consumer

    consumer[0]._cycle(scope)

    assert brain.batches == []
    assert buffer.scopes() == ()


# --------------------------------------------------------------------------
# buffer → brain
# --------------------------------------------------------------------------


def test_the_batch_is_the_whole_buffer_not_just_the_new_arrivals(
    consumer: tuple[SessionConsumer, _RecordingQueue, FakeBrain, FileEventBuffer], scope: Scope
) -> None:
    """「这一轮齐了没有」是跨批次的判断 —— 上一批剩下的还在 buffer 里，
    不在新到的那几条里。"""
    _, _, brain, buffer = consumer
    buffer.put(scope, "tool", (_tool(1),))
    buffer.put(scope, "tool", (_tool(2),))

    consumer[0]._cycle(scope)

    assert _ids(brain.batches[0].tool) == ["id-1", "id-2"]


def test_the_batch_carries_all_three_queues(
    consumer: tuple[SessionConsumer, _RecordingQueue, FakeBrain, FileEventBuffer], scope: Scope
) -> None:
    _, _, brain, buffer = consumer
    buffer.put(scope, "user", (_user(1),))
    buffer.put(scope, "tool", (_tool(2),))
    buffer.put(scope, "system", (_invoke(3),))

    consumer[0]._cycle(scope)

    batch = brain.batches[0]
    assert (_ids(batch.user), _ids(batch.tool), _ids(batch.system)) == (
        ["id-1"],
        ["id-2"],
        ["id-3"],
    )


# --------------------------------------------------------------------------
# brain → clear
# --------------------------------------------------------------------------


def test_a_settled_batch_clears_the_buffer(
    consumer: tuple[SessionConsumer, _RecordingQueue, FakeBrain, FileEventBuffer], scope: Scope
) -> None:
    _, queue, brain, buffer = consumer
    queue.put(scope, _user(1))

    consumer[0]._cycle(scope)

    assert brain.batches
    assert buffer.scopes() == ()


def test_an_unsettled_batch_keeps_the_buffer(
    consumer: tuple[SessionConsumer, _RecordingQueue, FakeBrain, FileEventBuffer], scope: Scope
) -> None:
    """核心契约：**brain 说不算，这一批就还在。** 下一轮醒来拿到的是同一批。"""
    session, queue, _, buffer = consumer
    unsettled = FakeBrain(settled=False)
    session._brain = unsettled
    queue.put(scope, _user(1))

    session._cycle(scope)

    assert unsettled.batches
    assert _ids(buffer.snapshot(scope, "user")) == ["id-1"]


def test_the_next_round_sees_the_same_batch(
    consumer: tuple[SessionConsumer, _RecordingQueue, FakeBrain, FileEventBuffer], scope: Scope
) -> None:
    session, _, _, buffer = consumer
    buffer.put(scope, "tool", (_tool(1),))
    unsettled = FakeBrain(settled=False)
    session._brain = unsettled

    session._cycle(scope)
    session._cycle(scope)

    assert len(unsettled.batches) == 2
    assert _ids(unsettled.batches[1].tool) == ["id-1"]


def test_a_settled_batch_does_not_wait(
    consumer: tuple[SessionConsumer, _RecordingQueue, FakeBrain, FileEventBuffer], scope: Scope
) -> None:
    """处理完了直接回 1 —— 没有要等的东西，也就不必问等多久。"""
    session, _, brain, buffer = consumer
    buffer.put(scope, "tool", (_tool(1),))

    session._cycle(scope)

    assert brain.delays_asked == 0
    assert consumer[1].timeouts == []


def test_clearing_touches_all_three_queues(
    consumer: tuple[SessionConsumer, _RecordingQueue, FakeBrain, FileEventBuffer], scope: Scope
) -> None:
    """一批里三样东西是同一件事 —— 只清一半就等于把另一半丢了。"""
    _, _, _, buffer = consumer
    buffer.put(scope, "user", (_user(1),))
    buffer.put(scope, "tool", (_tool(2),))
    buffer.put(scope, "system", (_invoke(3),))

    consumer[0]._cycle(scope)

    assert buffer.scopes() == ()


# --------------------------------------------------------------------------
# 等多久
# --------------------------------------------------------------------------


def test_an_empty_round_waits_the_idle_time(
    consumer: tuple[SessionConsumer, _RecordingQueue, FakeBrain, FileEventBuffer], scope: Scope
) -> None:
    """兜底：队列空着也醒一次看一眼，重启捞回来的那批才有机会被处理。"""
    session, queue, brain, _ = consumer

    session._cycle(scope)

    assert brain.batches == []
    assert len(queue.timeouts) == 1
    assert queue.timeouts[0] > 0.0


def test_an_unsettled_round_waits_what_the_brain_says(
    consumer: tuple[SessionConsumer, _RecordingQueue, FakeBrain, FileEventBuffer], scope: Scope
) -> None:
    """等多久是 brain 的判断 —— 这一层只会照着用。"""
    session, queue, _, buffer = consumer
    session._brain = FakeBrain(settled=False, delay=12.5)
    buffer.put(scope, "tool", (_tool(1),))

    session._cycle(scope)

    assert queue.timeouts == [12.5]


def test_a_failed_step_keeps_the_buffer_and_waits(
    consumer: tuple[SessionConsumer, _RecordingQueue, FakeBrain, FileEventBuffer], scope: Scope
) -> None:
    """一条输入处理失败不能让线程死掉 —— 剩下的输入还在队列里，失败的已经由
    `AgentBrain` 记成 `system.agent_step{error}`。buffer 不清，下一轮重试。"""
    session, queue, _, buffer = consumer

    class _Exploding(FakeBrain):
        def step(self, scope: Scope, batch: Batch) -> bool:
            raise RuntimeError("这一步炸了")

    session._brain = _Exploding()
    buffer.put(scope, "user", (_user(1),))

    session._cycle(scope)

    assert _ids(buffer.snapshot(scope, "user")) == ["id-1"]
    assert len(queue.timeouts) == 1


# --------------------------------------------------------------------------
# 起线程
# --------------------------------------------------------------------------


def _run(
    journal: Journal, brain: Brain, buffer: FileEventBuffer, queue: InMemoryEventQueue, scope: Scope
) -> tuple[threading.Thread, threading.Event]:
    ctx = agent_ctx(journal, queue=queue)
    stop = threading.Event()
    thread = threading.Thread(target=SessionConsumer(ctx, brain, buffer).run, args=(scope, stop))
    thread.start()
    return thread, stop


def test_run_picks_up_what_a_crash_left_behind(
    journal: Journal, buffer: FileEventBuffer, scope: Scope
) -> None:
    """崩在 step 中途的时候队列是空的，只有 buffer 记得有活没干完。

    `run` 起来第一件事就是捞它，不等新的 `put` 来叫醒。
    """
    brain = FakeBrain()
    queue = InMemoryEventQueue()
    buffer.put(scope, "user", (_user(1),))

    thread, stop = _run(journal, brain, buffer, queue, scope)
    assert _wait_for(lambda: bool(brain.batches))
    stop.set()
    queue.close(wait=False)
    thread.join(5.0)

    assert _ids(brain.batches[0].user) == ["id-1"]


def test_run_wakes_on_a_new_put(journal: Journal, buffer: FileEventBuffer, scope: Scope) -> None:
    brain = FakeBrain()
    queue = InMemoryEventQueue()

    thread, stop = _run(journal, brain, buffer, queue, scope)
    queue.put(scope, _user(1))
    assert _wait_for(lambda: bool(brain.batches))
    stop.set()
    queue.close(wait=False)
    thread.join(5.0)

    assert _ids(brain.batches[0].user) == ["id-1"]


def test_run_stops_when_stop_is_already_set(
    journal: Journal, buffer: FileEventBuffer, scope: Scope
) -> None:
    brain = FakeBrain()
    queue = InMemoryEventQueue()

    thread, stop = _run(journal, brain, buffer, queue, scope)
    stop.set()
    queue.close(wait=False)
    thread.join(5.0)

    assert not thread.is_alive()
    assert brain.batches == []


def test_run_leaves_nothing_behind_after_a_clean_round(
    journal: Journal, buffer: FileEventBuffer, scope: Scope
) -> None:
    brain = FakeBrain()
    queue = InMemoryEventQueue()

    thread, stop = _run(journal, brain, buffer, queue, scope)
    queue.put(scope, _user(1))
    assert _wait_for(lambda: bool(brain.batches))
    stop.set()
    queue.close(wait=False)
    thread.join(5.0)

    assert buffer.scopes() == ()
