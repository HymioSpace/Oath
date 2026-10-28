import time

import pytest

from bootstrap.clients.tool_client import ToolClient
from core.journal.runtime_store import RuntimeStore
from core.protocol.datatype import (
    PermissionDecision,
    PermissionRequest,
    ToolCallRequest,
    ToolDefinition,
    ToolResult,
    User,
)
from core.protocol.error import ToolRequestError
from core.protocol.interface import PermissionPolicy, Tool
from core.storage.adapters import DurableEventSink, StoredQueue, StoredTraces
from core.tool.impl import ThreadedToolSystem
from core.tool.registry import ToolRegistry


class Reviewer(PermissionPolicy):
    def check(self, request: PermissionRequest):
        return PermissionDecision(
            "ask",
            reason="Synthetic risky action",
            policy_version="fixture-v1",
            grant_key="synthetic-grant",
            approval={
                "title": "Run fixture?",
                "summary": "A synthetic approval",
                "risk": "high",
                "effects": ["execute"],
                "target": {"kind": "device", "id": "mac", "label": "Mac"},
                "scope_options": ["once", "run"],
            },
        )


class Probe(Tool):
    def __init__(self):
        self.executions = 0

    @property
    def definition(self):
        return ToolDefinition("probe", "fixture", {"type": "object"})

    def execute(self, invocation):
        self.executions += 1
        return ToolResult({"executions": self.executions})


def wait_for(fn):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("Timed out")


def test_approval_binds_digest_and_run_grant_reuses_exact_action(service):
    registry = ToolRegistry(default_timeout_seconds=10)
    probe = Probe()
    registry.register(probe)
    user = User("authorization-user")
    tools = ThreadedToolSystem(
        registry,
        DurableEventSink(service),
        Reviewer(),
        store=RuntimeStore(service, patch_commits=True, read_projection="tools"),
        queue=StoredQueue(service),
        traces=StoredTraces(service),
        max_concurrency=1,
    )
    try:
        tools.submit_batch(
            user,
            "first-batch",
            [
                ToolCallRequest(
                    "probe",
                    {"action": "run", "target": "mac"},
                    "first-call",
                    invocation_id="first-call",
                    run_id="run",
                )
            ],
        )
        waiting = wait_for(
            lambda: (
                value
                if (value := tools.status(user, "first-call")).status == "waiting_approval"
                else None
            )
        )
        assert waiting.status == "waiting_approval"
        digest = waiting.approval["action_digest"]
        with pytest.raises(ToolRequestError):
            tools.apply_permission(
                user,
                "first-call",
                "approved",
                "bad-decision",
                action_digest="f" * 64,
            )
        assert tools.apply_permission(
            user,
            "first-call",
            "approved",
            "decision",
            scope="run",
            client_id="iphone",
            channel="ios",
            action_digest=digest,
        )
        first = wait_for(
            lambda: (
                value if (value := tools.status(user, "first-call")).status == "succeeded" else None
            )
        )
        assert first.permission_decision["scope"] == "run"
        tools.submit_batch(
            user,
            "second-batch",
            [
                ToolCallRequest(
                    "probe",
                    {"action": "run", "target": "mac"},
                    "second-call",
                    invocation_id="second-call",
                    run_id="run",
                )
            ],
        )
        second = wait_for(
            lambda: (
                value
                if (value := tools.status(user, "second-call")).status == "succeeded"
                else None
            )
        )
        assert second.result.content == {"executions": 2}
    finally:
        tools.shutdown()


def test_expired_approval_becomes_a_terminal_permission_expiry(service):
    registry = ToolRegistry(default_timeout_seconds=10)
    probe = Probe()
    registry.register(probe)
    user = User("authorization-expiry")
    tools = ThreadedToolSystem(
        registry,
        DurableEventSink(service),
        Reviewer(),
        store=RuntimeStore(service, patch_commits=True, read_projection="tools"),
        queue=StoredQueue(service),
        traces=StoredTraces(service),
        max_concurrency=1,
        approval_ttl=0.05,
    )
    try:
        tools.submit_batch(
            user,
            "expiry-batch",
            [
                ToolCallRequest(
                    "probe",
                    {"action": "run"},
                    "expiry-call",
                    invocation_id="expiry-call",
                    run_id="expiry-run",
                )
            ],
        )
        wait_for(lambda: tools.status(user, "expiry-call").status == "waiting_approval")
        time.sleep(0.1)
        assert tools.apply_permission(
            user,
            "expiry-call",
            "expired",
            "approval-expired",
        )
        expired = wait_for(
            lambda: (
                value if (value := tools.status(user, "expiry-call")).status == "failed" else None
            )
        )
        assert expired.permission_decision["status"] == "expired"
        assert expired.error is not None
        assert expired.error.kind == "permission_expired"
        assert probe.executions == 0
    finally:
        tools.shutdown()


def test_tool_client_forwards_the_complete_permission_decision():
    class RecordingClient:
        def __init__(self):
            self.operation = ""
            self.params = {}

        def call(self, operation, **params):
            self.operation, self.params = operation, params
            return True

    recording = RecordingClient()
    ToolClient(recording).apply_permission(  # type: ignore[arg-type]
        User("permission-user"),
        "invocation",
        "denied",
        "command",
        scope="once",
        client_id="iphone",
        channel="ios",
        reason="Not this time",
        action_digest="a" * 64,
    )
    assert recording.operation == "tools.permission"
    assert recording.params["action_digest"] == "a" * 64
    assert recording.params["client_id"] == "iphone"
    assert recording.params["channel"] == "ios"
    assert recording.params["reason"] == "Not this time"
