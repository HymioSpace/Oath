"""审批状态由工具生命周期共享的事务保存，不能与执行资格分开。"""

from pathlib import Path

import pytest

from core.journal.runtime_store import State
from core.protocol.datatype import Scope, ToolCallRequest
from core.protocol.error import ToolRequestError
from core.utils.encoding import JsonObject
from tests._support.intake_fixtures import Harness, requests, until
from tests._support.tool_fixtures import EchoTool, VerdictPolicy


def pending(h: Harness) -> tuple[State, ...]:
    h.tools.execute(h.scope, requests("a"))
    return until(lambda: h.tools.permissions.pending(h.scope))


def test_pending_query_is_scoped_and_detached(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")], policy=VerdictPolicy(default="ask")) as h:
        pending(h)
        result = h.tools.permissions.get(h.scope, "a")
        assert result is not None
        result["arguments"]["forged"] = True
        again = h.tools.permissions.get(h.scope, "a")
        assert again is not None
        assert again["arguments"] == {}
        assert not h.tools.permissions.pending(Scope("other", h.scope.session_id))


def test_allow_and_deny_are_not_approval_requests(tmp_path: Path) -> None:
    with Harness(
        tmp_path, tools=[EchoTool("a"), EchoTool("b")], policy=VerdictPolicy({"b": "deny"})
    ) as h:
        h.tools.execute(h.scope, requests("a", "b"))
        until(lambda: h.call("b")["terminal"])
        assert not h.tools.permissions.pending(h.scope)
        assert not h.sink.permission_events()


def test_policy_does_not_mutate_original_arguments(tmp_path: Path) -> None:
    class Policy(VerdictPolicy):
        def check(self, scope: Scope, tool: str, args: JsonObject) -> str:
            args["mutated"] = True
            return "ask"

    with Harness(tmp_path, tools=[EchoTool("a")], policy=Policy()) as h:
        pending(h)
        assert h.call("a")["arguments"] == {}


@pytest.mark.parametrize("decision", ["approved", "denied", "expired"])
def test_decision_resolves_once_and_retains_original_arguments(
    tmp_path: Path, decision: str
) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")], policy=VerdictPolicy(default="ask")) as h:
        pending(h)
        assert h.tools.apply_permission(h.scope, "a", decision, "decision1")
        assert not h.tools.apply_permission(h.scope, "a", "denied", "decision2")
        assert h.call("a")["permission"] == decision
        assert not h.tools.permissions.pending(h.scope)
        assert h.call("a")["arguments"] == {}


def test_command_id_conflict_rejected(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")], policy=VerdictPolicy(default="ask")) as h:
        pending(h)
        h.tools.apply_permission(h.scope, "a", "denied", "same")
        with pytest.raises(ToolRequestError):
            h.tools.apply_permission(h.scope, "a", "approved", "same")


def test_invalid_decision_rejected(tmp_path: Path) -> None:
    with Harness(tmp_path) as h, pytest.raises(ToolRequestError):
        h.tools.apply_permission(h.scope, "missing", "pending", "cmd")


def test_original_arguments_cannot_be_replaced_while_awaiting_approval(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")], policy=VerdictPolicy(default="ask")) as h:
        pending(h)
        with pytest.raises(ToolRequestError):
            h.tools.execute(
                h.scope, [ToolCallRequest("a", {"amount": 999}, "provider-a", "a", "b1", "r1")]
            )
        assert h.call("a")["arguments"] == {}


def test_idle_approval_expiry_goes_through_system_consumer(tmp_path: Path) -> None:
    with Harness(
        tmp_path, tools=[EchoTool("a")], policy=VerdictPolicy(default="ask"), approval_ttl=0.05
    ) as h:
        pending(h)
        until(lambda: any(e["queue"] == "system" for e in h.state()["inputs"].values()))
        assert h.call("a")["status"] == "waiting_approval"
        h.consumers[2].consume(h.scope)
        assert h.call("a")["permission"] == "expired"
        assert h.call("a")["terminal"]["source"] == "system"
        assert h.call("a")["started_dt"] is None
