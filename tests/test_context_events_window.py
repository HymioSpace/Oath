import copy
import threading
import time
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from bootstrap.gateway.process import validate
from core.agent.brain import AgentBrain
from core.compaction.coordinator import CompactionCoordinator
from core.compaction.llm import LlmCompactor, _parse
from core.context.intake import build_messages
from core.context.policy import effective_window
from core.journal.runtime_store import RuntimeStore, initial_state
from core.protocol.datatype.config import AgentConfig, ModelProviderConfig
from core.protocol.datatype.inbound import Inbound, of_command
from core.protocol.datatype.llm import LlmResponse
from core.protocol.datatype.user import User
from core.protocol.datatype.wire import Command
from core.protocol.interface.compaction import CompactionError
from core.utils.tokens import estimate


class CaptureCompactor:
    def __init__(self):
        self.inputs = []

    def compress(self, messages, *, budget_tokens):
        self.inputs.append(copy.deepcopy(messages))
        return [
            {
                "role": "user",
                "content": "<context_summary>约束：保留编号42；继续完成报告。</context_summary>",
            }
        ]


def history(count, size=1000):
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "文" * size}
        for i in range(count)
    ]


def setup(service, count):
    store, user = RuntimeStore(service), User("synthetic")
    store.transact(user, "fixture", lambda s: s["conversation"].update(messages=history(count)))
    algorithm = CaptureCompactor()
    coordinator = CompactionCoordinator(store, algorithm, AgentConfig(), workers=1)
    return store, user, algorithm, coordinator


def test_events_aggregate_escape_and_keep_occurrence_metadata():
    state = initial_state()
    entries = []
    for i, (kind, source, payload) in enumerate(
        [
            (
                "user",
                "user.message",
                {
                    "content": "<event>不是标签</event>",
                    "user_context": {"secret": "hidden"},
                    "dynamic": {
                        "location": {"latitude": 31.2, "longitude": 121.5},
                        "timezone": "Asia/Shanghai",
                    },
                },
            ),
            ("system", "app.task.due", {"task_id": "t42", "content": "整理报告"}),
            ("tool", "subagent.start", {"content": "核实数字"}),
        ]
    ):
        item = Inbound(
            kind, source, payload, str(i), "2026-10-24T01:02:03Z", {"secret": "trace-hidden"}
        )
        entries.append((str(i), {"queue": kind, "item": item.to_dict(), "buffered_at": 1}))
    messages = build_messages(state, entries, "step")
    assert len(messages) == 1
    root = ET.fromstring(messages[0]["content"])
    assert len(root.findall("event")) == 3
    event = root[0]
    assert event.attrib == {"type": "user.message", "id": "user:0"}
    assert event.findtext("content") == "<event>不是标签</event>"
    assert event.findtext("dynamic/time") == "2026-10-24T01:02:03Z"
    assert "31.2" in event.findtext("dynamic/context")
    assert "hidden" not in messages[0]["content"]


def test_native_tool_result_is_not_wrapped_or_detached():
    state = initial_state()
    state["conversation"]["messages"] = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c", "name": "x", "arguments": {}}],
        }
    ]
    state["batches"]["b"] = {"presented_by_step": None, "members": ["i"]}
    state["calls"]["i"] = {"invocation_id": "i", "tool_call_id": "c", "status": "succeeded"}
    item = Inbound("tool", "tool.terminal", {"invocation_id": "i", "content": "<data>raw</data>"})
    messages = build_messages(state, [("k", {"queue": "tool", "item": item.to_dict()})], "s")
    assert messages[-1] == {"role": "tool", "tool_call_id": "c", "content": "<data>raw</data>"}


def test_window_defaults_cap_and_ingress():
    assert AgentConfig().context_window == 256000
    assert effective_window(256000, "MiniMax-M2.7") == 204800
    assert effective_window(1000000, "Qwen/Qwen3.8-27B") == 262144
    assert effective_window(1000000, "Qwen/Qwen3.8-27B", {"Qwen/Qwen3.8-27B": 512000}) == 512000
    assert effective_window(256000, "unknown") == 128000
    assert effective_window(1000000, "MiniMax-M2.7", {"MiniMax-M2.7": 1000000}) == 204800
    for value in (True, 0, "128000", 200000):
        with pytest.raises(ValueError):
            validate(
                Command(
                    "m",
                    "user.message",
                    {"content": "x", "user_context": {}, "context_window": value},
                )
            )
    command = Command(
        "m",
        "user.message",
        {
            "content": "x",
            "user_context": {},
            "context_window": 128000,
            "dynamic": {"timezone": "UTC"},
        },
    )
    validate(command)
    assert of_command(command).payload["context_window"] == 128000
    assert of_command(command).payload["dynamic"] == {"timezone": "UTC"}


def test_async_only_prepares_until_configured_sync_threshold(service):
    store, user, algorithm, coordinator = setup(service, 60)
    try:
        coordinator.prepare(user, context_window=128000)
        coordinator._pool.submit(lambda: None).result(timeout=5)
        state = store.read(user)
        assert state["compactions"]["pending"]["mode"] == "async"
        assert "applied" not in state["compactions"]
        assert len(coordinator.view(state["conversation"]["messages"], state)) == 60
        assert len(algorithm.inputs) == 1
        # Below 90%: don't apply the prepared summary, don't recompress.
        coordinator.prepare(user, context_window=128000)
        assert "applied" not in store.read(user)["compactions"]
        store.transact(user, "more", lambda s: s["conversation"]["messages"].extend(history(60)))
        coordinator.prepare(user, context_window=128000)
        state = store.read(user)
        assert state["compactions"]["applied"]["covers"] == 48
        assert len(state["conversation"]["messages"]) == 120
        assert estimate(coordinator.view(state["conversation"]["messages"], state)) < 90000
    finally:
        coordinator.shutdown()


def test_sync_compresses_cold_history_and_incrementally_reuses_digest(service):
    store, user, algorithm, coordinator = setup(service, 128)
    try:
        coordinator.prepare(user, context_window=128000)
        state = store.read(user)
        artifact = state["compactions"]["applied"]
        assert artifact["mode"] == "sync" and artifact["context_window"] == 128000
        assert artifact["input_tokens"] > 115200 and artifact["output_tokens"] < 1024
        assert len(coordinator.view(state["conversation"]["messages"], state)) == 13
        store.transact(user, "more", lambda s: s["conversation"]["messages"].extend(history(110)))
        coordinator.prepare(user, context_window=128000)
        assert algorithm.inputs[-1][0]["content"].startswith("<context_summary>")
        assert len(algorithm.inputs[-1]) < 130
        assert len(store.read(user)["conversation"]["messages"]) == 238
    finally:
        coordinator.shutdown()


def test_256k_does_not_compress_same_history_that_crosses_128k(service):
    store, user, algorithm, coordinator = setup(service, 60)
    try:
        coordinator.prepare(user, context_window=256000)
        assert not algorithm.inputs
        assert not store.read(user)["compactions"]
    finally:
        coordinator.shutdown()


def test_compression_input_chunks_respect_compression_model_capacity():
    class Provider:
        def __init__(self):
            self.requests = []

        def complete(self, request, **kwargs):
            self.requests.append(request)
            return LlmResponse(
                content="<context_digest><summary>完成42项。</summary><facts><item>编号42</item></facts><open><item>继续报告</item></open></context_digest>"
            )

    provider = Provider()
    compactor = LlmCompactor(provider, model="small", max_output_tokens=1024, context_window=8000)
    result = compactor.compress(history(8, 3000), budget_tokens=1024)
    assert len(provider.requests) > 1
    assert all(
        estimate([{"content": m.content} for m in r.messages]) + r.max_output_tokens < 8000
        for r in provider.requests
    )
    assert "编号42" in result[0]["content"]
    assert "context_summary" in provider.requests[1].messages[1].content


def test_request_freezes_effective_window_and_survives_retry(service):
    store, user, _algorithm, coordinator = setup(service, 128)
    calls = []
    sink = SimpleNamespace(emit=lambda e: None)
    ctx = SimpleNamespace(
        store=store,
        config=AgentConfig(provider=ModelProviderConfig(model="MiniMax-M3")),
        context_provider=lambda u: calls.append(u) or "Overall understanding",
        sink=sink,
    )
    brain = AgentBrain(ctx, None, coordinator)
    step = {
        "step_id": "s",
        "run_id": "r",
        "trace_id": "0" * 32,
        "status": "prepared",
        "request": None,
        "context_window": 128000,
        "request_draft": {
            "messages": [{"role": "system", "content": "Rules"}]
            + store.read(user)["conversation"]["messages"],
            "tools": [],
            "model": "MiniMax-M3",
            "max_output_tokens": 2048,
        },
    }
    store.transact(user, "draft", lambda s: s["steps"].update(s=step))
    service.call("wake", user_id=user.storage_id, force=True)
    store.fence = service.call("brain.claim", owner="test", ttl=60)
    try:
        prepared = brain._prepare_request(user, step)
        assert prepared["context_usage"]["window"] == 128000
        assert prepared["context_usage"]["estimated_tokens"] < 128000
        original = copy.deepcopy(prepared["request"])
        again = brain._prepare_request(user, prepared)
        assert again["request"] == original and len(calls) == 1
        assert "context_window" not in original
    finally:
        brain.close()
        coordinator.shutdown()


def test_inline_provider_thinking_does_not_discard_a_valid_digest():
    xml = "<context_digest><summary>完成。</summary><facts/><open/></context_digest>"
    assert _parse("<think>reasoning <xml> is not output</think>\n" + xml)["content"].startswith(
        "<context_summary>"
    )
    assert _parse("<think>incomplete" + xml) is None
    assert _parse("extra text " + xml) is None
    assert _parse("<different><summary>bad root</summary></different>") is None


def test_sync_reuses_pending_that_finished_while_waiting_for_lease(service):
    store, user, algorithm, coordinator = setup(service, 128)
    try:
        coordinator._compress(user, applied=False, context_window=128000)
        assert len(algorithm.inputs) == 1
        coordinator._compress(user, applied=True, context_window=128000)
        assert len(algorithm.inputs) == 1
        coordinator._apply_pending(user)
        assert store.read(user)["compactions"]["applied"]["covers"] == 116
    finally:
        coordinator.shutdown()


def test_sync_compaction_never_waits_for_another_process_lease(service):
    _store, user, algorithm, coordinator = setup(service, 128)
    fence = service.call(
        "lease.acquire",
        user_id=user.storage_id,
        lane="compaction",
        owner="other-worker",
        ttl=60,
    )
    try:
        started = time.monotonic()
        coordinator._compress(user, applied=True, context_window=128000)
        assert time.monotonic() - started < 0.5
        assert algorithm.inputs == []
    finally:
        service.call(
            "lease.release",
            user_id=user.storage_id,
            fence=fence,
        )
        coordinator.shutdown()


def test_prepare_does_not_wait_for_local_async_compaction(service):
    class BlockingCompactor:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.inputs = []

        def compress(self, messages, *, budget_tokens):
            self.inputs.append(copy.deepcopy(messages))
            self.started.set()
            assert self.release.wait(5)
            return [{"role": "user", "content": "short summary"}]

    store, user = RuntimeStore(service), User("blocking-async")
    store.transact(user, "fixture", lambda s: s["conversation"].update(messages=history(60)))
    algorithm = BlockingCompactor()
    coordinator = CompactionCoordinator(store, algorithm, AgentConfig(), workers=1)
    try:
        coordinator.prepare(user, context_window=128000)
        assert algorithm.started.wait(2)
        store.transact(
            user,
            "more",
            lambda s: s["conversation"]["messages"].extend(history(60)),
        )
        started = time.monotonic()
        coordinator.prepare(user, context_window=128000)
        assert time.monotonic() - started < 0.5
        assert len(algorithm.inputs) == 1
    finally:
        algorithm.release.set()
        coordinator.shutdown()


def test_compaction_failure_has_reason_and_short_circuits_retry(service):
    class FailingCompactor:
        def __init__(self):
            self.calls = 0

        def compress(self, messages, *, budget_tokens):
            self.calls += 1
            raise CompactionError("invalid_output")

    events = []
    store, user = RuntimeStore(service), User("failing-compaction")
    store.transact(user, "fixture", lambda s: s["conversation"].update(messages=history(128)))
    algorithm = FailingCompactor()
    coordinator = CompactionCoordinator(
        store,
        algorithm,
        AgentConfig(),
        sink=SimpleNamespace(emit=events.append),
        workers=1,
        retry_seconds=60,
    )
    try:
        coordinator.prepare(
            user,
            context_window=128000,
            trace_id="a" * 32,
            step_id="step-a",
        )
        assert algorithm.calls == 1
        assert events[-1].payload["status"] == "failed"
        assert events[-1].payload["reason"] == "invalid_output"
        assert events[-1].payload["trace_id"] == "a" * 32
        assert events[-1].payload["step_id"] == "step-a"
        coordinator.prepare(
            user,
            context_window=128000,
            trace_id="b" * 32,
            step_id="step-b",
        )
        assert algorithm.calls == 1
    finally:
        coordinator.shutdown()


def test_async_events_keep_trigger_trace_and_step(service):
    events = []
    _store, user, _algorithm, coordinator = setup(service, 60)
    coordinator._sink = SimpleNamespace(emit=events.append)
    try:
        coordinator.prepare(
            user,
            context_window=128000,
            trace_id="c" * 32,
            step_id="step-c",
            channel="ios",
        )
        coordinator._pool.submit(lambda: None).result(timeout=5)
        states = [(e.payload["status"], e.payload["mode"]) for e in events]
        assert ("started", "async") in states
        assert ("ready", "async") in states
        assert all(e.payload["trace_id"] == "c" * 32 for e in events)
        assert all(e.payload["step_id"] == "step-c" for e in events)
        assert all(e.payload["channel"] == "ios" for e in events)
    finally:
        coordinator.shutdown()


def test_async_completion_resumes_context_paused_step(service):
    store, user, _algorithm, coordinator = setup(service, 60)
    step_id = "2" * 16

    def pause(state):
        state["steps"][step_id] = {
            "step_id": step_id,
            "run_id": "r",
            "trace_id": "3" * 32,
            "status": "paused",
            "pause_reason": "context_window",
            "error": "内容较多",
            "attempts": 0,
            "input_ids": [],
            "trigger": ["user"],
        }
        state["meta"]["active_step"] = step_id

    store.transact(user, "fixture.pause", pause)
    try:
        coordinator.prepare(user, context_window=128000)
        coordinator._pool.submit(lambda: None).result(timeout=5)
        step = store.read(user)["steps"][step_id]
        assert step["status"] == "prepared"
        assert step["pause_reason"] is None
        assert step["error"] is None
    finally:
        coordinator.shutdown()


def test_intake_freezes_selected_model_window_and_continuations(service):
    from core.intake.coordinator import IntakeCoordinator

    store = RuntimeStore(service)
    user = User("intake-synthetic")
    config = AgentConfig(
        provider=ModelProviderConfig(model="MiniMax-M3"), selectable_models=("MiniMax-M2.7",)
    )
    item = Inbound(
        "user",
        "user.message",
        {"content": "test", "model": "MiniMax-M2.7", "context_window": 512000},
        "message",
        "2026-10-24T00:00:00Z",
    )
    buffer = {"user": [item]}
    ctx = SimpleNamespace(
        store=store,
        config=config,
        tools=SimpleNamespace(definitions=lambda: ()),
        buffer=SimpleNamespace(peek=lambda u, q: buffer.get(q, [])),
    )
    executor = SimpleNamespace(
        system_prompt=lambda: "Rules", open_step=lambda *args: None, emit=lambda *args: None
    )
    service.call("wake", user_id=user.storage_id, force=True)
    store.fence = service.call("brain.claim", owner="intake-test", ttl=60)
    intake = IntakeCoordinator(ctx, executor, threading.Event())
    step = intake.claim(user)
    assert step["context_window"] == 204800
    assert step["request_draft"]["model"] == "MiniMax-M2.7"
    assert store.read(user)["meta"]["selected_context_window"] == 512000
    store.transact(
        user,
        "fixture.finish",
        lambda s: (
            s["steps"][step["step_id"]].update(status="completed"),
            s["meta"].update(active_step=None),
        ),
    )
    buffer.clear()
    buffer["system"] = [
        Inbound("system", "app.task.due", {"task_id": "t"}, "due", "2026-10-24T00:01:00Z")
    ]
    continued = intake.claim(user)
    assert continued["context_window"] == 204800
    assert continued["request_draft"]["model"] == "MiniMax-M2.7"


def test_sync_threshold_is_configurable(service):
    from dataclasses import replace

    store, user, algorithm, coordinator = setup(service, 80)
    coordinator._config = replace(
        AgentConfig(), compress_async_at_ratio=0.3, compress_sync_at_ratio=0.6
    )
    try:
        coordinator.prepare(user, context_window=128000)
        assert store.read(user)["compactions"]["applied"]
        assert algorithm.inputs
    finally:
        coordinator.shutdown()


def test_oversized_recent_input_pauses_without_provider_request(service):
    store, user, _, coordinator = setup(service, 1)
    spans = []
    brain = AgentBrain(
        SimpleNamespace(
            store=store,
            config=AgentConfig(),
            context_provider=None,
            sink=SimpleNamespace(emit=lambda e: None),
            traces=SimpleNamespace(append=lambda _user, span: spans.append(span)),
        ),
        None,
        coordinator,
    )
    messages = [{"role": "user", "content": "文" * 130000}]
    store.transact(user, "fixture", lambda s: s["conversation"].update(messages=messages))
    step = {
        "step_id": "1" * 16,
        "run_id": "r",
        "trace_id": "0" * 32,
        "trace_started": time.time_ns(),
        "input_ids": [],
        "trigger": ["user"],
        "status": "prepared",
        "request": None,
        "context_window": 128000,
        "request_draft": {
            "messages": messages,
            "tools": [],
            "model": "MiniMax-M3",
            "max_output_tokens": 2048,
        },
    }
    store.transact(
        user,
        "fixture.step",
        lambda s: s["steps"].update({step["step_id"]: step}),
    )
    service.call("wake", user_id=user.storage_id, force=True)
    store.fence = service.call("brain.claim", owner="pause", ttl=60)
    try:
        value = brain._prepare_request(user, step)
        assert value["status"] == "paused" and value["request"] is None
        assert value["request_draft"]["messages"] == messages
        assert spans and spans[-1].end_unix_nano is not None
        assert spans[-1].status == "unset"
    finally:
        brain.close()
        coordinator.shutdown()


def test_failed_async_summary_keeps_complete_history(service):
    store, user, algorithm, coordinator = setup(service, 60)
    algorithm.compress = lambda *args, **kwargs: None
    try:
        before = store.read(user)["conversation"]["messages"]
        coordinator.prepare(user, context_window=128000)
        coordinator._pool.submit(lambda: None).result(timeout=5)
        state = store.read(user)
        assert not state["compactions"]
        assert state["conversation"]["messages"] == before
    finally:
        coordinator.shutdown()


def test_vendor_parameters_cannot_override_frozen_output_budget_or_model():
    from core.llm.openai_provider import build_payload
    from core.protocol.datatype.llm import LlmRequest
    from core.protocol.datatype.message import Message

    request = LlmRequest(
        messages=[Message("user", "data")],
        model="chosen",
        max_output_tokens=512,
        extra_params={"max_tokens": 1000000, "model": "other", "messages": [], "stream": True},
    )
    payload = build_payload(request, "chosen")
    assert payload["max_tokens"] == 512 and payload["model"] == "chosen"
    assert payload["messages"] == [{"role": "user", "content": "data"}]
    assert payload["stream"] is False
