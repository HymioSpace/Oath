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


class ManagedReviewer(Reviewer):
    def check(self, request: PermissionRequest):
        return PermissionDecision(
            "ask",
            reason="Synthetic policy-managed action",
            policy_version="fixture-v2",
            grant_key="synthetic-managed-grant",
            approval={
                "title": "Run managed fixture?",
                "summary": "A policy-managed synthetic approval",
                "risk": "high",
                "tier": "write",
                "effects": ["execute"],
                "target": {"kind": "device", "id": "mac", "label": "Mac"},
                "scope_options": ["once", "run", "exact_rule"],
                "policy_managed_grant": True,
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


class RecordingQueue:
    def __init__(self):
        self.items = []

    def add(self, user, item):
        self.items.append(item)


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


def test_late_approval_notifies_agent_and_grants_one_safe_retry(service):
    registry = ToolRegistry(default_timeout_seconds=10)
    probe = Probe()
    registry.register(probe)
    user = User("authorization-late-approval")
    queue = RecordingQueue()
    notifications = []
    tools = ThreadedToolSystem(
        registry,
        DurableEventSink(service),
        Reviewer(),
        store=RuntimeStore(service, patch_commits=True, read_projection="tools"),
        queue=queue,
        traces=StoredTraces(service),
        max_concurrency=1,
        approval_ttl=10,
        approval_notifier=lambda _, payload: notifications.append(payload),
    )
    try:
        tools.submit_batch(
            user,
            "late-batch",
            [
                ToolCallRequest(
                    "probe",
                    {"action": "run"},
                    "late-call",
                    invocation_id="late-call",
                    run_id="late-run",
                )
            ],
        )
        waiting = wait_for(
            lambda: (
                value
                if (value := tools.status(user, "late-call")).status == "waiting_approval"
                else None
            )
        )
        assert notifications == [
            {
                "user_id": "authorization-late-approval",
                "invocation_id": "late-call",
                "title": "Run fixture?",
                "summary": "A synthetic approval",
            }
        ]
        assert tools.apply_permission(
            user,
            "late-call",
            "expired",
            "late-expired",
        )
        expired = wait_for(
            lambda: value if (value := tools.status(user, "late-call")).status == "failed" else None
        )
        assert expired.error is not None
        assert expired.error.kind == "permission_expired"
        assert probe.executions == 0

        assert tools.apply_permission(
            user,
            "late-call",
            "approved",
            "late-approved",
            scope="once",
            client_id="iphone",
            channel="ios",
            action_digest=waiting.approval["action_digest"],
        )
        corrected = tools.status(user, "late-call")
        assert corrected is not None
        assert corrected.status == "failed"
        assert corrected.error.kind == "permission_expired"
        assert corrected.late_permission["status"] == "approved"
        assert probe.executions == 0
        assert queue.items[-1].source == "permission.late_resolved"
        assert queue.items[-1].payload["executed"] is False
        assert queue.items[-1].payload["retry_safe"] is True
        queued_notifications = len(queue.items)
        assert not tools.apply_permission(
            user,
            "late-call",
            "denied",
            "late-conflict",
            scope="once",
            client_id="web",
            channel="web",
            action_digest=waiting.approval["action_digest"],
        )
        assert len(queue.items) == queued_notifications

        tools.submit_batch(
            user,
            "retry-batch",
            [
                ToolCallRequest(
                    "probe",
                    {"action": "run"},
                    "retry-call",
                    invocation_id="retry-call",
                    run_id="late-run",
                )
            ],
        )
        retried = wait_for(
            lambda: (
                value if (value := tools.status(user, "retry-call")).status == "succeeded" else None
            )
        )
        assert retried.result.content == {"executions": 1}
        assert retried.retry_of == "late-call"

        tools.submit_batch(
            user,
            "second-retry-batch",
            [
                ToolCallRequest(
                    "probe",
                    {"action": "run"},
                    "second-retry-call",
                    invocation_id="second-retry-call",
                    run_id="late-run",
                )
            ],
        )
        wait_for(lambda: tools.status(user, "second-retry-call").status == "waiting_approval")
        assert probe.executions == 1
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


def test_policy_managed_exact_rules_are_not_duplicated_in_runtime_grants(service):
    registry = ToolRegistry(default_timeout_seconds=10)
    registry.register(Probe())
    user = User("authorization-managed-rule")
    tools = ThreadedToolSystem(
        registry,
        DurableEventSink(service),
        ManagedReviewer(),
        store=RuntimeStore(service, patch_commits=True, read_projection="tools"),
        queue=StoredQueue(service),
        traces=StoredTraces(service),
        max_concurrency=1,
    )
    try:
        tools.submit_batch(
            user,
            "managed-first",
            [
                ToolCallRequest(
                    "probe",
                    {"action": "same"},
                    "managed-first",
                    invocation_id="managed-first",
                    run_id="managed-run",
                )
            ],
        )
        waiting = wait_for(
            lambda: (
                value
                if (value := tools.status(user, "managed-first")).status == "waiting_approval"
                else None
            )
        )
        assert tools.apply_permission(
            user,
            "managed-first",
            "approved",
            "managed-decision",
            scope="exact_rule",
            action_digest=waiting.approval["action_digest"],
        )
        wait_for(lambda: tools.status(user, "managed-first").status == "succeeded")
        tools.submit_batch(
            user,
            "managed-second",
            [
                ToolCallRequest(
                    "probe",
                    {"action": "same"},
                    "managed-second",
                    invocation_id="managed-second",
                    run_id="managed-run",
                )
            ],
        )
        managed = wait_for(
            lambda: (
                value
                if (value := tools.status(user, "managed-second")).status == "waiting_approval"
                else None
            )
        )
        assert managed.status == "waiting_approval"
    finally:
        tools.shutdown()
