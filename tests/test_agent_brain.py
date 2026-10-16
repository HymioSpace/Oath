"""真实 event-driven 链路、模型配对以及请求固定后的并发到达。"""

import threading
from pathlib import Path

from core.context.intake import validate_messages
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.llm import LlmRequest, LlmResponse, LlmToolCall
from core.protocol.datatype.queue import QueueName
from tests._support.intake_fixtures import Harness, arrived, until
from tests._support.tool_fixtures import EchoTool, GatedTool, VerdictPolicy


def call_response(*names: str) -> LlmResponse:
    return LlmResponse(tool_calls=tuple(LlmToolCall(n, {}, f"id-{n}") for n in names))


def test_single_allow_roundtrip(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")], responses=[call_response("a")]) as h:
        h.start().send()
        request = h.provider.wait(2)
        validate_messages([m.to_dict() for m in request.messages])
        assert request.messages[-1].role == "tool"
        assert request.messages[-1].extra["tool_call_id"] == "id-a"
        until(lambda: h.state()["meta"].get("active_step") is None)
        assert all(e["phase"] == "done" for e in h.state()["inputs"].values())


def test_single_ask_approval_executes_original_invocation(tmp_path: Path) -> None:
    with Harness(
        tmp_path,
        tools=[EchoTool("a")],
        policy=VerdictPolicy(default="ask"),
        responses=[call_response("a")],
    ) as h:
        h.start().send()
        call = until(
            lambda: h.call("a") and h.call("a")["status"] == "waiting_approval" and h.call("a")
        )
        assert len(h.provider.requests) == 1
        assert not h.sink.tool_events()
        h.decide(call["invocation_id"])
        request = h.provider.wait(2)
        assert request.messages[-1].role == "tool"
        assert len(h.calls()) == 1
        assert h.call("a")["source"] == "system"


def test_mixed_batch_including_fast_approval(tmp_path: Path) -> None:
    a, b = GatedTool("a"), GatedTool("b")
    with Harness(
        tmp_path,
        tools=[a, b, EchoTool("c"), EchoTool("d"), EchoTool("e")],
        policy=VerdictPolicy({"c": "ask", "d": "ask", "e": "ask"}),
        responses=[call_response("a", "b", "c", "d", "e")],
    ) as h:
        try:
            h.start().send()
            until(lambda: h.call("c") and h.call("c")["status"] == "waiting_approval")
            a.gate.set()
            h.decide(h.call("c")["invocation_id"])
            until(lambda: h.call("a")["terminal"] and h.call("c")["terminal"])
            assert len(h.provider.requests) == 1
            b.gate.set()
            request = h.provider.wait(2)
            results = [m for m in request.messages if m.role == "tool"]
            assert len(results) == 5
            assert "等待用户批准" in results[3].content
            assert "等待用户批准" in results[4].content
            validate_messages([m.to_dict() for m in request.messages])
        finally:
            a.gate.set()
            b.gate.set()


def test_user_interrupt_freezes_placeholder_before_answer_and_late_result_is_user(
    tmp_path: Path,
) -> None:
    gate = threading.Event()
    b = GatedTool("b")

    def hold(request: LlmRequest) -> LlmResponse:
        assert gate.wait(3)
        return LlmResponse(content="先处理你的消息")

    with Harness(tmp_path, tools=[b], responses=[call_response("b"), hold]) as h:
        try:
            h.start().send()
            until(lambda: b.entered == 1)
            h.send("先说另一件事", "u2")
            second = h.provider.wait(2)
            tool_message = next(m for m in second.messages if m.role == "tool")
            assert "尚未返回" in tool_message.content
            b.gate.set()
            assert len(h.provider.requests) == 2
            assert (
                next(m for m in second.messages if m.role == "tool").content == tool_message.content
            )
            gate.set()
            third = h.provider.wait(3)
            assert third.messages[-1].role == "user"
            assert "<tool_result " in third.messages[-1].content
            assert (
                next(m for m in third.messages if m.role == "tool").content == tool_message.content
            )
            validate_messages([m.to_dict() for m in third.messages])
        finally:
            gate.set()
            b.gate.set()


def test_denial_after_user_interruption_is_late_notice(tmp_path: Path) -> None:
    with Harness(
        tmp_path,
        tools=[EchoTool("a")],
        policy=VerdictPolicy(default="ask"),
        responses=[call_response("a")],
    ) as h:
        h.start().send()
        until(lambda: h.call("a") and h.call("a")["status"] == "waiting_approval")
        h.send("先聊其他", "u2")
        h.provider.wait(2)
        h.decide(h.call("a")["invocation_id"], "denied")
        third = h.provider.wait(3)
        assert third.messages[-1].role == "user" and "denied" in third.messages[-1].content
        assert h.call("a")["started_dt"] is None


def test_failed_request_retries_identical_input_and_preserves_new_input(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    def fail(request: LlmRequest) -> LlmResponse:
        entered.set()
        assert release.wait(3)
        raise RuntimeError("临时错误")

    with Harness(tmp_path, responses=[fail, LlmResponse(content="恢复")]) as h:
        try:
            h.start().send()
            assert entered.wait(3)
            h.send("后来的消息", "u2")
            until(lambda: arrived(h, QueueName.USER))
            release.set()
            second = h.provider.wait(2)
            assert second == h.provider.requests[0]
            third = h.provider.wait(3)
            assert third.messages[-1].content == "后来的消息"
        finally:
            release.set()


def test_retry_exhaustion_keeps_claim_and_needs_explicit_retry(tmp_path: Path) -> None:
    with Harness(tmp_path, responses=[RuntimeError("坏配置")] * 3) as h:
        h.start().send()
        until(lambda: any(s["status"] == "failed" for s in h.state()["steps"].values()))
        h.send("新消息", "u2")
        until(lambda: arrived(h, QueueName.USER))
        assert len(h.provider.requests) == 3
        assert any(e["phase"] == "claimed" for e in h.state()["inputs"].values())
        item = Inbound(QueueName.SYSTEM, "step.retry", {}, "retry1")
        h.submit(item)
        h.provider.wait(5)
        assert h.provider.requests[3] == h.provider.requests[0]
        h.submit(item)
        until(lambda: h.state()["meta"].get("active_step") is None)
        assert len(h.provider.requests) == 5


def test_repeated_provider_call_id_in_different_batches_is_safe(tmp_path: Path) -> None:
    with Harness(
        tmp_path, tools=[EchoTool("a")], responses=[call_response("a"), call_response("a")]
    ) as h:
        h.start().send()
        request = h.provider.wait(3)
        assert len(h.calls()) == 2
        validate_messages([m.to_dict() for m in request.messages])
        assert len([m for m in request.messages if m.role == "tool"]) == 2


def test_duplicate_call_ids_in_one_response_execute_nothing(tmp_path: Path) -> None:
    bad = LlmResponse(tool_calls=(LlmToolCall("a", {}, "same"), LlmToolCall("a", {}, "same")))
    with Harness(tmp_path, tools=[EchoTool("a")], responses=[bad] * 3) as h:
        h.start().send()
        until(lambda: any(s["status"] == "failed" for s in h.state()["steps"].values()))
        assert not h.calls()


def test_duplicate_user_command_does_not_ask_again(tmp_path: Path) -> None:
    with Harness(tmp_path) as h:
        h.start()
        item = h.send()
        h.provider.wait(1)
        until(lambda: h.state()["meta"].get("active_step") is None)
        h.submit(item)
        h.consumers[0].deliver(h.scope)
        h.consumers[0].evaluate(h.scope, True)
        assert len(h.provider.requests) == 1
