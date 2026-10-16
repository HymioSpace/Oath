"""三个独立 consumer 的唤醒规则；不调用模型来验证队列决策。"""

import time
from pathlib import Path

import pytest

from core.journal.runtime_store import State
from core.protocol.datatype import Scope
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import QueueName
from core.protocol.interface.brain import Brain
from tests._support.intake_fixtures import Harness, requests, until
from tests._support.tool_fixtures import EchoTool, GatedTool, VerdictPolicy


class RecordingBrain(Brain):
    """只记录被唤醒过，不真的跑 step。

    `wake` 不再带触发来源 —— 判定属于 consumer 自己。所以每个 consumer 挂一个带名字的
    实例，靠它回答「是谁叫的」。
    """

    def __init__(self, label: str = "?") -> None:
        self.label, self.wakes = label, 0

    def wake(self, scope: Scope) -> None:
        self.wakes += 1

    def resume(self, scope: Scope) -> None:
        pass

    def retry(self, scope: Scope) -> None:
        pass

    def close(self, *, wait: bool = True) -> None:
        pass


class NamedBrains:
    """三条队列各挂一个记录器，测试里按名字问「谁叫的」。"""

    def __init__(self, harness: Harness) -> None:
        self.by_queue = {
            consumer.queue_name: RecordingBrain(consumer.queue_name)
            for consumer in harness.consumers
        }
        for consumer in harness.consumers:
            consumer.brain = self.by_queue[consumer.queue_name]

    def woke(self, queue: str) -> bool:
        return self.by_queue[queue].wakes > 0

    def only(self, queue: str) -> bool:
        """只有这一条队列叫过。"""
        return self.woke(queue) and all(
            brain.wakes == 0 for name, brain in self.by_queue.items() if name != queue
        )


def mixed(tmp_path: Path) -> tuple[Harness, NamedBrains, GatedTool, GatedTool]:
    a, b, c = GatedTool("a"), GatedTool("b"), EchoTool("c")
    h = Harness(tmp_path, tools=[a, b, c], policy=VerdictPolicy({"c": "ask"}))
    brains = NamedBrains(h)
    h.tools.execute(h.scope, requests("a", "b", "c"))
    until(lambda: h.batch("b1")["admitted"])
    return h, brains, a, b


def receive(h: Harness) -> None:
    """收一轮 tool 结果。"""
    h.consumers[1].deliver(h.scope)


def expire(h: Harness) -> None:
    """把 b1 的工具批次窗口推到已过期。不动窗口长度 —— 判定靠的是 deadline。"""

    def age(state: State) -> None:
        batch = state["batches"]["b1"]
        batch.update(
            first_buffered_at=time.time() - 2,
            buffer_deadline=time.time() - 1,
            buffer_window=1.0,
        )

    h.store.transact(h.scope, "test.elapsed", age)


def test_system_result_cannot_bypass_initial_submissions(tmp_path: Path) -> None:
    h, brains, a, b = mixed(tmp_path)
    try:
        a.gate.set()
        until(lambda: h.call("a")["terminal"])
        receive(h)
        h.decide("c")
        h.consumers[2].deliver(h.scope)
        h.consumers[2].evaluate(h.scope, True)
        until(lambda: h.call("c")["terminal"])
        receive(h)
        h.consumers[1].evaluate(h.scope, True)
        # 审批结果不是工具结果。它不能让模型在初次提交还在跑的时候开新一轮。
        assert not brains.woke(QueueName.TOOL)
        assert h.call("c")["terminal"]["source"] == "system"
        assert h.batch("b1")["initial_submitted"] == ["a", "b"]
        b.gate.set()
        until(lambda: h.call("b")["terminal"])
        receive(h)
        h.consumers[1].evaluate(h.scope, True)
        assert brains.woke(QueueName.TOOL)
    finally:
        a.gate.set()
        b.gate.set()
        h.close()


def test_worker_done_is_not_buffer_visibility(tmp_path: Path) -> None:
    """工具线程跑完不等于结果到了队列。没人收走就不该叫模型。"""
    h, brains, a, b = mixed(tmp_path)
    try:
        a.gate.set()
        until(lambda: h.call("a")["terminal"])
        receive(h)
        b.gate.set()
        until(lambda: h.call("b")["terminal"])
        h.consumers[1].evaluate(h.scope, False)
        assert not brains.woke(QueueName.TOOL)
        receive(h)
        h.consumers[1].evaluate(h.scope, True)
        assert brains.woke(QueueName.TOOL)
    finally:
        a.gate.set()
        b.gate.set()
        h.close()


def test_redelivery_does_not_extend_the_window(tmp_path: Path) -> None:
    """窗口从第一条结果进 buffer 起算。同一条结果重投不重开窗口 ——
    否则一个反复投递的来源能让工具批次永远等不齐。"""
    h, brains, a, b = mixed(tmp_path)
    try:
        a.gate.set()
        until(lambda: h.call("a")["terminal"])
        h.consumers[1].deliver(h.scope)
        opened = h.batch("b1")["buffer_deadline"]
        redelivery = Inbound.from_dict(
            {"kind": QueueName.TOOL, "source": "tool.terminal", **h.call("a")["terminal"]}
        )

        def shift(state: State) -> None:
            batch = state["batches"]["b1"]
            batch.update(
                first_buffered_at=opened - batch["buffer_window"] - 1, buffer_deadline=opened - 1
            )

        h.store.transact(h.scope, "test.elapsed", shift)
        h.consumers[1].receive(h.scope, redelivery)  # 同一条结果重投
        h.consumers[1].evaluate(h.scope, True)

        assert h.batch("b1")["buffer_deadline"] == opened - 1
        assert brains.woke(QueueName.TOOL)
        assert not h.call("b")["terminal"]
    finally:
        a.gate.set()
        b.gate.set()
        h.close()


@pytest.mark.parametrize("decision", ["approved", "denied", "expired"])
def test_waiting_for_a_human_does_not_hold_the_window(tmp_path: Path, decision: str) -> None:
    """等审批的调用不占着工具窗口：一个回来了就该叫模型 —— 等人不是等工具。"""
    with Harness(
        tmp_path, tools=[EchoTool("a"), EchoTool("b")], policy=VerdictPolicy(default="ask")
    ) as h:
        brains = NamedBrains(h)
        h.tools.execute(h.scope, requests("a", "b"))
        until(lambda: h.batch("b1")["admitted"])
        receive(h)
        h.consumers[1].evaluate(h.scope, True)
        assert not brains.woke(QueueName.TOOL) and not h.sink.tool_events()

        h.tools.apply_permission(h.scope, "a", decision, "decision")
        until(lambda: h.call("a")["terminal"])
        receive(h)
        h.consumers[1].evaluate(h.scope, True)

        assert brains.woke(QueueName.TOOL)
        assert h.call("b")["status"] == "waiting_approval"


def test_a_still_running_call_holds_the_window(tmp_path: Path) -> None:
    """还在跑的那个才是要等的。窗口没到之前不叫模型 —— 早叫会让它读半截结果。"""
    a, b = GatedTool("a"), GatedTool("b")
    with Harness(tmp_path, tools=[a, b]) as h:
        try:
            h.tools.execute(h.scope, requests("a", "b"))
            until(lambda: h.batch("b1")["admitted"])
            a.gate.set()
            until(lambda: h.call("a")["terminal"])
            h.consumers[1].deliver(h.scope)
            brains = NamedBrains(h)

            assert not brains.woke(QueueName.TOOL)
            assert 0 < h.consumers[1].evaluate(h.scope, True) <= h.batch("b1")["buffer_window"]

            b.gate.set()
            until(lambda: h.call("b")["terminal"])
            h.consumers[1].deliver(h.scope)
            h.consumers[1].evaluate(h.scope, True)
            assert brains.woke(QueueName.TOOL)
        finally:
            a.gate.set()
            b.gate.set()


def test_an_expired_window_releases_a_batch_that_never_settles(tmp_path: Path) -> None:
    """窗口是兜底：批次没收齐也到点了，就把手上的放出来，别让输入卡在 buffer 里。"""
    a, b = GatedTool("a"), GatedTool("b")
    with Harness(tmp_path, tools=[a, b]) as h:
        try:
            h.tools.execute(h.scope, requests("a", "b"))
            until(lambda: h.batch("b1")["admitted"])
            a.gate.set()
            until(lambda: h.call("a")["terminal"])
            h.consumers[1].deliver(h.scope)
            brains = NamedBrains(h)
            h.consumers[1].evaluate(h.scope, True)
            assert not brains.woke(QueueName.TOOL)

            expire(h)
            h.consumers[1].evaluate(h.scope, False)
            assert brains.woke(QueueName.TOOL)
        finally:
            a.gate.set()
            b.gate.set()


def test_user_and_system_have_independent_rules(tmp_path: Path) -> None:
    with Harness(tmp_path) as h:
        brains = NamedBrains(h)
        h.send()
        h.consumers[2].deliver(h.scope)
        h.consumers[2].evaluate(h.scope, False)
        assert not brains.woke(QueueName.SYSTEM)
        h.consumers[0].deliver(h.scope)
        h.consumers[0].evaluate(h.scope, True)
        assert brains.only(QueueName.USER)
        h.submit(Inbound(QueueName.SYSTEM, "system.invoke", {"note": "继续"}, "tick"))
        h.consumers[2].deliver(h.scope)
        h.consumers[2].evaluate(h.scope, True)
        assert brains.by_queue[QueueName.SYSTEM].wakes == 1


def test_invoke_is_not_an_input(tmp_path: Path) -> None:
    """`system.invoke` 只要求「看一眼 buffer」，它自己不是输入：不下 buffer、不进 journal。"""
    with Harness(tmp_path) as h:
        brains = NamedBrains(h)
        h.submit(Inbound(QueueName.SYSTEM, "system.invoke", {}, "tick"))
        h.consumers[2].deliver(h.scope)
        h.consumers[2].evaluate(h.scope, True)

        assert brains.woke(QueueName.SYSTEM)
        assert h.state()["inputs"] == {}
        assert h.state()["meta"].get("active_step") is None


def test_listen_only_watches_its_own_queue(tmp_path: Path) -> None:
    with Harness(tmp_path) as h:
        h.send()
        assert not h.queue.listen(h.scope, QueueName.TOOL, timeout=0.01)
        assert h.queue.listen(h.scope, QueueName.USER, timeout=0.01)


def test_a_deadline_bounds_the_next_listen(tmp_path: Path) -> None:
    """没有新输入时也要定期复查 —— 否则工具批次的窗口永远等不到到期。"""
    a, b = GatedTool("a"), GatedTool("b")
    with Harness(tmp_path, tools=[a, b]) as h:
        try:
            h.tools.execute(h.scope, requests("a", "b"))
            until(lambda: h.batch("b1")["admitted"])
            a.gate.set()
            until(lambda: h.call("a")["terminal"])
            h.consumers[1].deliver(h.scope)

            window = h.batch("b1")["buffer_window"]
            # 没到期：先按住，但 listen 不能长过窗口。
            assert 0 < h.consumers[1].evaluate(h.scope, True) <= window

            expire(h)
            assert h.consumers[1].evaluate(h.scope, False) > 0
        finally:
            a.gate.set()
            b.gate.set()
