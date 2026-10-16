"""跨模块边界：并发认领、有限重试、取消与可恢复时钟。"""

import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from core.consumer import input_key
from core.journal.runtime_store import RuntimeStore
from core.protocol.datatype.config import AgentConfig
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.llm import LlmRequest, LlmResponse
from core.protocol.datatype.queue import QueueName
from core.utils.deadlines import Deadlines
from tests._support.intake_fixtures import Harness, requests, until
from tests._support.tool_fixtures import EchoTool, VerdictPolicy
from tests.test_agent_brain import call_response


def test_concurrent_wakes_claim_all_buffers_once(tmp_path: Path) -> None:
    """三个 consumer 同时叫，Brain 只认领一次 —— 一个 step 里两条输入。"""
    with Harness(tmp_path) as h:
        h.queue.add(h.scope, Inbound(QueueName.USER, "user.message", {"content": "hi"}, "u"))
        h.queue.add(h.scope, Inbound(QueueName.USER, "user.message", {"content": "check"}, "s"))
        for consumer in h.consumers:
            consumer.deliver(h.scope)
        barrier = threading.Barrier(3)

        def wake() -> None:
            barrier.wait()
            h.brain.wake(h.scope)

        threads = [threading.Thread(target=wake) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)

        h.provider.wait(1)
        until(lambda: h.state()["meta"].get("active_step") is None)
        h.brain.wake(h.scope)  # 认领完了再叫是空的
        assert len(h.state()["steps"]) == 1
        assert all(entry["phase"] == "done" for entry in h.state()["inputs"].values())
        assert len(h.state()["inputs"]) == 2


def test_an_input_claimed_twice_keeps_one_step(tmp_path: Path) -> None:
    """同一条输入重投（崩在写 buffer 之后）不算新的事实。"""
    with Harness(tmp_path) as h:
        item = Inbound(QueueName.USER, "user.message", {"content": "hi"}, "u")
        h.queue.add(h.scope, item)
        h.consumers[0].deliver(h.scope)
        h.brain.wake(h.scope)
        h.provider.wait(1)
        until(lambda: h.state()["meta"].get("active_step") is None)

        h.queue.add(h.scope, item)
        h.consumers[0].deliver(h.scope)
        h.brain.wake(h.scope)
        until(lambda: not h.queue.listen(h.scope, QueueName.USER, 0.05))

        assert len(h.state()["steps"]) == 1
        assert list(h.state()["inputs"]) == [input_key(item)]
        assert len(h.provider.requests) == 1


def test_approval_expires_without_another_user_message(tmp_path: Path) -> None:
    with Harness(
        tmp_path,
        tools=[EchoTool("a")],
        policy=VerdictPolicy(default="ask"),
        responses=[call_response("a")],
        approval_ttl=0.05,
    ) as h:
        h.start().send()
        request = h.provider.wait(2)
        assert "expired" in request.messages[-1].content
        assert request.messages[-1].role == "tool"
        assert h.call("a")["started_dt"] is None


def test_postprocessing_failure_is_bounded_and_retry_reuses_saved_response(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")], responses=[call_response("a")]) as h:
        submit = h.tools.submit_batch
        with patch.object(
            h.tools, "submit_batch", side_effect=RuntimeError("dispatch failed")
        ) as bad:
            h.start().send()
            until(lambda: any(s["status"] == "failed" for s in h.state()["steps"].values()))
            assert bad.call_count == h.config.llm_max_attempts
            assert len(h.provider.requests) == 1
        assert h.tools.submit_batch == submit
        h.submit(Inbound(QueueName.SYSTEM, "step.retry", {}, "retry"))
        h.provider.wait(2)
        assert len(h.calls()) == 1
        assert h.provider.requests[1] != h.provider.requests[0]


def test_run_budget_pauses_endless_tool_generation(tmp_path: Path) -> None:
    with Harness(
        tmp_path,
        tools=[EchoTool("a")],
        responses=[call_response("a")] * 4,
        config=AgentConfig(max_run_steps=2),
    ) as h:
        h.start().send()
        until(lambda: any(s["status"] == "paused" for s in h.state()["steps"].values()))
        assert len(h.provider.requests) == 2
        assert len(h.calls()) == 2


def test_cancel_while_model_running_prevents_dispatch(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    def reply(request: LlmRequest) -> LlmResponse:
        entered.set()
        assert release.wait(3)
        return call_response("a")

    with Harness(tmp_path, tools=[EchoTool("a")], responses=[reply]) as h:
        try:
            h.start().send()
            assert entered.wait(3)
            item = Inbound(QueueName.SYSTEM, "run.cancel", {}, "cancel")
            h.submit(item)
            h.consumers[2].deliver(h.scope)
            until(lambda: input_key(item) in h.state()["controls"])
            first_run = h.state()["meta"]["run_id"]
            release.set()
            until(lambda: h.state()["meta"].get("active_step") is None)
            assert not h.calls()

            h.send("new run", "new")
            until(lambda: len(h.state()["inputs"]) >= 2)
            new_run = h.state()["meta"]["run_id"]
            assert first_run != new_run
            # 控制命令重放：已经 applied 的不能把新 run 也取消掉。
            h.consumers[2].apply(h.scope, item)
            assert not h.state()["meta"].get(f"cancelled:{new_run}")
        finally:
            release.set()


def test_monotonic_deadline_ignores_wall_clock_changes() -> None:
    with (
        patch("core.utils.deadlines.time.time", return_value=100) as wall,
        patch("core.utils.deadlines.time.monotonic", return_value=10) as mono,
    ):
        deadlines = Deadlines()
        assert deadlines.remaining("x", 110, 10) == 10
        wall.return_value = -1000
        mono.return_value = 15
        assert deadlines.remaining("x", 110, 10) == 5
        mono.return_value = 20
        assert deadlines.remaining("x", 110, 10) == 0
        # 新进程遭遇回拨，至多恢复一个窗口，不能等 1110 秒。
        assert Deadlines().remaining("x", 110, 10) == 10


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True])
def test_deadline_configuration_must_be_finite_positive(value: float) -> None:
    with pytest.raises(ValueError):
        AgentConfig(tool_round_timeout_seconds=value)


def test_store_preserves_null_and_rejects_accidental_deletion(tmp_path: Path) -> None:
    with Harness(tmp_path) as h:
        h.store.transact(h.scope, "null", lambda s: s["meta"].update(optional=None))
        assert "optional" in RuntimeStore(h.journal).read(h.scope)["meta"]
        with pytest.raises(ValueError, match="不可删除"):
            h.store.transact(h.scope, "bad", lambda s: s["meta"].pop("optional"))
        assert "optional" in h.state()["meta"]


def test_a_forged_tool_result_never_reaches_the_buffer(tmp_path: Path) -> None:
    """队列通知不能伪造调用或改写终态。进 buffer 就是让模型读一条不存在的工具结果。"""
    with Harness(tmp_path, tools=[EchoTool("a")]) as h:
        h.tools.execute(h.scope, requests("a"))
        until(lambda: h.call("a")["terminal"])
        forged = Inbound(
            QueueName.TOOL,
            "tool.terminal",
            {"invocation_id": ["invalid"], "batch_id": "unknown"},
            "forged",
        )
        h.submit(forged)
        h.consumers[1].deliver(h.scope)

        # a 的真结果照常进 buffer，伪造的那条不到场。
        assert [item.source_id for item in h.buffer.get_all(QueueName.TOOL)] == ["result:a"]
        assert h.state()["inputs"] == {}
        assert any(event.type == "intake.rejected" for event in h.sink.events)


def test_a_valid_result_still_gets_through_alongside_a_forged_one(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")]) as h:
        h.tools.execute(h.scope, requests("a"))
        until(lambda: h.call("a")["terminal"])
        h.submit(
            Inbound(
                QueueName.TOOL,
                "tool.terminal",
                {"invocation_id": ["invalid"], "batch_id": "unknown"},
                "forged",
            )
        )
        h.consumers[1].deliver(h.scope)

        buffered = h.buffer.get_all(QueueName.TOOL)
        assert [item.source_id for item in buffered] == ["result:a"]
