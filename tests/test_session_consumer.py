"""三个独立 consumer 的唤醒规则；不调用模型来验证队列决策。"""

import time
from pathlib import Path

import pytest

from core.buffer.journal_buffer import input_id
from core.journal.runtime_store import State
from core.protocol.datatype import Scope
from core.protocol.datatype.inbound import Inbound, InboundKind
from core.protocol.interface.brain import Brain
from tests._support.intake_fixtures import Harness, requests, until
from tests._support.tool_fixtures import EchoTool, GatedTool, VerdictPolicy


class RecordingBrain(Brain):
    """只记录被唤醒过，不真的跑 step。"""

    def __init__(self) -> None:
        self.wakes: list[tuple[str, tuple[str, ...]]] = []

    def wake(self, scope: Scope, trigger: str, event_ids: tuple[str, ...]) -> None:
        self.wakes.append((trigger, event_ids))

    def resume(self, scope: Scope) -> None:
        pass

    def retry(self, scope: Scope) -> None:
        pass

    def close(self, *, wait: bool = True) -> None:
        pass


def mixed(tmp_path: Path) -> tuple[Harness, RecordingBrain, GatedTool, GatedTool]:
    a, b, c = GatedTool("a"), GatedTool("b"), EchoTool("c")
    h = Harness(tmp_path, tools=[a, b, c], policy=VerdictPolicy({"c": "ask"}))
    brain = RecordingBrain()
    for consumer in h.consumers:
        consumer.brain = brain
    h.tools.execute(h.scope, requests("a", "b", "c"))
    until(lambda: h.batch("b1")["admitted"])
    return h, brain, a, b


def receive(h: Harness) -> None:
    h.consumers[1].consume(h.scope)


def test_system_result_cannot_bypass_initial_submissions(tmp_path: Path) -> None:
    h, brain, a, b = mixed(tmp_path)
    try:
        a.gate.set()
        until(lambda: h.call("a")["terminal"])
        receive(h)
        h.decide("c")
        h.consumers[2].consume(h.scope)
        h.consumers[2].evaluate(h.scope)
        until(lambda: h.call("c")["terminal"])
        receive(h)
        h.consumers[1].evaluate(h.scope)
        assert not brain.wakes
        assert h.call("c")["terminal"]["source"] == "system"
        assert h.batch("b1")["initial_submitted"] == ["a", "b"]
        b.gate.set()
        until(lambda: h.call("b")["terminal"])
        receive(h)
        h.consumers[1].evaluate(h.scope)
        assert brain.wakes[-1][0] == "tool"
    finally:
        a.gate.set()
        b.gate.set()
        h.close()


def test_worker_done_is_not_buffer_visibility(tmp_path: Path) -> None:
    h, brain, a, b = mixed(tmp_path)
    try:
        a.gate.set()
        until(lambda: h.call("a")["terminal"])
        receive(h)
        b.gate.set()
        until(lambda: h.call("b")["terminal"])
        h.consumers[1].evaluate(h.scope)
        assert not brain.wakes
        receive(h)
        h.consumers[1].evaluate(h.scope)
        assert brain.wakes
    finally:
        a.gate.set()
        b.gate.set()
        h.close()


def test_timeout_uses_first_buffer_receipt_and_dedup_does_not_extend_it(tmp_path: Path) -> None:
    h, brain, a, b = mixed(tmp_path)
    try:
        a.gate.set()
        until(lambda: h.call("a")["terminal"])
        receive(h)
        first = h.buffers.snapshot(h.scope, "tool")[0]
        receipt = h.state()["inputs"][input_id(first)]["buffered_at"]
        h.buffers.receive(h.scope, first)
        assert h.state()["inputs"][input_id(first)]["buffered_at"] == receipt

        def age(s: State) -> None:
            s["batches"]["b1"]["buffer_deadline"] = time.time() - 2

        h.store.transact(h.scope, "test.elapsed", age)
        h.consumers[1].evaluate(h.scope)
        assert brain.wakes
        assert not h.call("b")["terminal"]
    finally:
        a.gate.set()
        b.gate.set()
        h.close()


@pytest.mark.parametrize("decision", ["approved", "denied", "expired"])
def test_all_ask_no_empty_wake_then_first_terminal_wakes(tmp_path: Path, decision: str) -> None:
    with Harness(
        tmp_path, tools=[EchoTool("a"), EchoTool("b")], policy=VerdictPolicy(default="ask")
    ) as h:
        brain = RecordingBrain()
        h.consumers[1].brain = brain
        h.tools.execute(h.scope, requests("a", "b"))
        until(lambda: h.batch("b1")["admitted"])
        receive(h)
        h.consumers[1].evaluate(h.scope)
        assert not brain.wakes and not h.sink.tool_events()
        h.tools.apply_permission(h.scope, "a", decision, "decision")
        until(lambda: h.call("a")["terminal"])
        receive(h)
        h.consumers[1].evaluate(h.scope)
        assert brain.wakes
        assert h.call("b")["status"] == "waiting_approval"


def test_presented_batch_late_result_wakes_without_remaining_initial_result(tmp_path: Path) -> None:
    h, brain, a, b = mixed(tmp_path)
    try:
        h.store.transact(
            h.scope,
            "test.presented",
            lambda s: s["batches"]["b1"].update(presented_by_step="user-step"),
        )
        a.gate.set()
        until(lambda: h.call("a")["terminal"])
        receive(h)
        h.consumers[1].evaluate(h.scope)
        assert brain.wakes
    finally:
        a.gate.set()
        b.gate.set()
        h.close()


def test_user_and_system_have_independent_rules(tmp_path: Path) -> None:
    with Harness(tmp_path) as h:
        brain = RecordingBrain()
        for c in h.consumers:
            c.brain = brain
        h.send()
        h.consumers[2].consume(h.scope)
        h.consumers[2].evaluate(h.scope)
        assert not brain.wakes
        h.consumers[0].consume(h.scope)
        h.consumers[0].evaluate(h.scope)
        assert brain.wakes[-1][0] == "user"
        h.submit(Inbound(InboundKind.INVOKE, "system.invoke", {"note": "继续"}, "tick"))
        h.consumers[2].consume(h.scope)
        h.consumers[2].evaluate(h.scope)
        assert brain.wakes[-1][0] == "system"


def test_queue_wait_does_not_wake_for_another_queue(tmp_path: Path) -> None:
    with Harness(tmp_path) as h:
        h.send()
        assert not h.queue.wait(h.scope, queue="tool", timeout=0.01)
        assert h.queue.wait(h.scope, queue="user", timeout=0.01)
