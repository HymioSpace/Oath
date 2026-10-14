"""统一 Journal 中的审批：失败原子性与重启后的请求身份。"""

from pathlib import Path
from typing import Any

import pytest

from tests._support.intake_fixtures import Harness, requests, until
from tests._support.tool_fixtures import EchoTool, VerdictPolicy


def test_pending_approval_reconstructs_from_common_journal(tmp_path: Path) -> None:
    h = Harness(tmp_path, tools=[EchoTool("a")], policy=VerdictPolicy(default="ask"))
    h.tools.execute(h.scope, requests("a"))
    original = until(lambda: h.tools.permissions.pending(h.scope))
    h.close()
    with Harness(tmp_path, tools=[EchoTool("a")]) as restored:
        assert restored.tools.permissions.pending(restored.scope) == original


def test_failed_decision_cannot_publish_execution_eligibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")], policy=VerdictPolicy(default="ask")) as h:
        h.tools.execute(h.scope, requests("a"))
        until(lambda: h.call("a")["status"] == "waiting_approval")
        original = h.journal.append

        def fail(*args: Any, **kwargs: Any) -> Any:
            if args[2].get("reason") == "permission.decided":
                raise OSError("持久化失败")
            return original(*args, **kwargs)

        monkeypatch.setattr(h.journal, "append", fail)
        with pytest.raises(OSError):
            h.tools.apply_permission(h.scope, "a", "approved", "approve")
        assert h.call("a")["status"] == "waiting_approval"
        assert h.call("a")["permission"] == "pending"
        assert not h.state()["controls"]
