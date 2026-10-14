"""批次接纳、唯一终态、权限原子提交及执行边界。"""

import threading
import time
from pathlib import Path

import pytest

from core.protocol.datatype import Scope, ToolCallRequest
from core.protocol.datatype.config import AgentConfig
from core.protocol.error import ToolRequestError
from core.utils.encoding import JsonObject
from tests._support.intake_fixtures import Harness, requests, until
from tests._support.tool_fixtures import EchoTool, GatedTool, RaisingTool, VerdictPolicy


def test_complete_manifest_before_any_worker_runs(tmp_path: Path) -> None:
    barrier = threading.Barrier(3)
    a, b = GatedTool("a", barrier=barrier), GatedTool("b", barrier=barrier)
    with Harness(tmp_path, tools=[a, b]) as h:
        try:
            h.tools.execute(h.scope, requests("a", "b"))
            barrier.wait(3)
            batch = h.batch("b1")
            assert batch["members"] == ["a", "b"]
            assert batch["initial_submitted"] == ["a", "b"]
            assert batch["admitted"]
        finally:
            a.gate.set()
            b.gate.set()


def test_admission_timeout_releases_other_classified_members(tmp_path: Path) -> None:
    gate = threading.Event()

    class Policy(VerdictPolicy):
        def check(self, scope: Scope, tool: str, arguments: JsonObject) -> str:
            if tool == "a":
                gate.wait(3)
            return "allow"

    with Harness(
        tmp_path, tools=[EchoTool("a"), EchoTool("b")], policy=Policy(), admission_timeout=0.05
    ) as h:
        try:
            h.tools.execute(h.scope, requests("a", "b"))
            until(lambda: h.call("b")["terminal"])
            assert h.call("a")["status"] == "failed"
            assert h.call("a")["started_dt"] is None
            assert h.batch("b1")["initial_submitted"] == ["b"]
        finally:
            gate.set()


def test_duplicate_dispatch_and_payload_conflict(tmp_path: Path) -> None:
    a = GatedTool("a")
    with Harness(tmp_path, tools=[a]) as h:
        try:
            h.tools.execute(h.scope, requests("a"))
            h.tools.execute(h.scope, requests("a"))
            until(lambda: a.entered == 1)
            with pytest.raises(ToolRequestError):
                h.tools.execute(
                    h.scope, [ToolCallRequest("a", {"changed": True}, "provider-a", "a", "b1")]
                )
            assert a.entered == 1
        finally:
            a.gate.set()


def test_duplicate_approval_runs_original_once(tmp_path: Path) -> None:
    a = GatedTool("a")
    with Harness(tmp_path, tools=[a], policy=VerdictPolicy(default="ask")) as h:
        try:
            h.tools.execute(h.scope, requests("a"))
            until(lambda: h.call("a")["status"] == "waiting_approval")
            threads = [
                threading.Thread(
                    target=h.tools.apply_permission, args=(h.scope, "a", "approved", f"approve-{i}")
                )
                for i in range(6)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(3)
            until(lambda: a.entered == 1)
            assert a.entered == 1
            assert h.call("a")["source"] == "system"
            assert not h.batch("b1")["initial_submitted"]
        finally:
            a.gate.set()


def test_approve_at_expiry_cannot_execute_even_without_timer(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")], policy=VerdictPolicy(default="ask")) as h:
        h.tools.execute(h.scope, requests("a"))
        until(lambda: h.call("a")["status"] == "waiting_approval")
        h.store.transact(
            h.scope,
            "test.expire",
            lambda s: s["calls"]["a"].update(approval_deadline=time.time() - 1),
        )
        h.tools.apply_permission(h.scope, "a", "approved", "approve")
        assert h.call("a")["permission"] == "expired"
        assert h.call("a")["started_dt"] is None


def test_cancelled_approval_never_dispatches(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")], policy=VerdictPolicy(default="ask")) as h:
        h.tools.execute(h.scope, requests("a"))
        until(lambda: h.call("a")["status"] == "waiting_approval")
        h.tools.cancel(h.scope, "r1")
        assert not h.tools.apply_permission(h.scope, "a", "approved", "approve")
        assert h.call("a")["status"] == "cancelled"
        assert h.call("a")["started_dt"] is None


def test_pending_approval_does_not_consume_worker(tmp_path: Path) -> None:
    with Harness(
        tmp_path,
        tools=[EchoTool("a"), EchoTool("b")],
        policy=VerdictPolicy({"a": "ask"}),
        max_concurrency=1,
    ) as h:
        h.tools.execute(h.scope, requests("a", "b"))
        until(lambda: h.call("b")["status"] == "succeeded")
        assert h.call("a")["status"] == "waiting_approval"


def test_queued_deadline_prevents_later_execution(tmp_path: Path) -> None:
    a, b = GatedTool("a"), GatedTool("b")
    with Harness(
        tmp_path, tools=[a, b], max_concurrency=1, config=AgentConfig(tool_timeout_seconds=0.07)
    ) as h:
        try:
            h.tools.execute(h.scope, requests("a", "b"))
            until(lambda: h.call("a")["terminal"] and h.call("b")["terminal"])
            a.gate.set()
            b.gate.set()
            assert h.call("a")["status"] == "timeout"
            assert h.call("b")["status"] == "timeout"
            assert a.entered + b.entered == 1
        finally:
            a.gate.set()
            b.gate.set()


def test_late_worker_result_cannot_change_terminal(tmp_path: Path) -> None:
    a = GatedTool("a")
    with Harness(tmp_path, tools=[a], config=AgentConfig(tool_timeout_seconds=0.06)) as h:
        try:
            h.tools.execute(h.scope, requests("a"))
            until(lambda: h.call("a")["status"] == "timeout")
            original = h.call("a")["terminal"]
            a.gate.set()
            until(lambda: a.finished)
            assert h.call("a")["terminal"] == original
        finally:
            a.gate.set()


def test_failed_tool_does_not_block_other_member(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[RaisingTool("a", ValueError("broken")), EchoTool("b")]) as h:
        h.tools.execute(h.scope, requests("a", "b"))
        until(lambda: h.call("a")["terminal"] and h.call("b")["terminal"])
        assert h.call("a")["status"] == "failed"
        assert h.call("b")["status"] == "succeeded"


def test_denied_call_is_durable_and_cannot_be_reused(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")], policy=VerdictPolicy(default="deny")) as h:
        h.tools.execute(h.scope, requests("a"))
        until(lambda: h.call("a")["terminal"])
        h.tools.execute(h.scope, requests("a"))
        assert h.call("a")["started_dt"] is None
        execution = h.tools.status(h.scope, "a")
        assert execution is not None
        assert execution.is_terminal
        assert h.call("a")["terminal"]["source"] == "brain"


def test_scope_isolation(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")]) as h:
        h.tools.execute(h.scope, requests("a"))
        other = Scope("other", h.scope.session_id)
        assert h.tools.status(other, "a") is None
        assert h.tools.batch_status(other, "b1") is None
        assert not h.tools.apply_permission(other, "a", "approved", "approve")


@pytest.mark.parametrize("verdict", ["garbage", "deny"])
def test_non_allow_policy_never_executes(tmp_path: Path, verdict: str) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")], policy=VerdictPolicy(default=verdict)) as h:
        h.tools.execute(h.scope, requests("a"))
        until(lambda: h.call("a")["terminal"])
        assert h.call("a")["started_dt"] is None


def test_shutdown_prevents_new_dispatch(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")]) as h:
        h.tools.shutdown()
        with pytest.raises(ToolRequestError):
            h.tools.execute(h.scope, requests("a"))
