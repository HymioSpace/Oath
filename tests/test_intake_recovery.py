"""持久化边界故障注入；请求、审批与执行资格在重启后保持身份。"""

import copy
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from core.journal.impl import FileJournal
from core.journal.runtime_store import RuntimeStore, State
from core.protocol.datatype import Cursor, CursorKind, EventType
from core.protocol.datatype.inbound import Inbound, InboundKind
from tests._support.intake_fixtures import SCOPE, Harness, requests, until
from tests._support.tool_fixtures import EchoTool, VerdictPolicy


def test_accepted_but_not_queued_input_recovers_without_client(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.buffers.accept(SCOPE, Inbound(InboundKind.USER, "user.message", {"content": "已接纳"}, "u1"))
    h.close()
    with Harness(tmp_path) as recovered:
        recovered.start()
        assert recovered.provider.wait(1).messages[-1].content == "已接纳"


def test_claim_all_is_one_commit_and_new_input_survives_completion(tmp_path: Path) -> None:
    with Harness(tmp_path) as h:
        h.send("first")
        h.consumers[0].consume(SCOPE)
        key = next(iter(h.state()["inputs"]))
        step = h.brain._claim(SCOPE, {"user": (key,)})
        assert step is not None
        h.send("second", "u2")
        h.consumers[0].consume(SCOPE)
        h.brain._execute(SCOPE, step)
        assert [i.payload["content"] for i in h.buffers.snapshot(SCOPE, "user")] == ["second"]
        records = list(h.journal.read_range(SCOPE, Cursor(CursorKind.LINE, 0)))
        claim = next(
            r
            for r in records
            if r.type == EventType.RUNTIME_COMMIT and r.payload["reason"] == "step.input"
        )
        changed = cast("dict[str, Any]", claim.payload["changes"])
        assert {"inputs", "steps", "meta", "conversation"} <= set(changed)


def test_failed_claim_has_no_partial_buffer_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Harness(tmp_path) as h:
        h.send()
        h.consumers[0].consume(SCOPE)
        original = h.journal.append

        def fail(*args: Any, **kwargs: Any) -> Any:
            if args[2].get("reason") == "step.input":
                raise OSError("磁盘故障")
            return original(*args, **kwargs)

        monkeypatch.setattr(h.journal, "append", fail)
        key = next(iter(h.state()["inputs"]))
        with pytest.raises(OSError):
            h.brain._claim(SCOPE, {"user": (key,)})
        assert not h.state()["steps"]
        assert h.state()["inputs"][key]["phase"] == "buffered"


def test_claim_committed_before_reported_failure_recovers_same_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.send()
    h.consumers[0].consume(SCOPE)
    original = h.journal.append

    def fail_after(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if args[2].get("reason") == "step.input":
            raise OSError("提交后进程中断")
        return result

    monkeypatch.setattr(h.journal, "append", fail_after)
    with pytest.raises(OSError):
        h.brain._claim(SCOPE, {"user": tuple(h.state()["inputs"])})
    saved = h.state()["meta"]["active_step"]
    h.close()
    with Harness(tmp_path) as restored:
        restored.start()
        restored.provider.wait(1)
        until(lambda: restored.state()["steps"][saved]["status"] == "completed")
        assert len(restored.state()["steps"]) == 1


def test_fixed_request_not_rebuilt_from_later_tool_or_user_state(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.send("原输入")
    h.consumers[0].consume(SCOPE)
    step = h.brain._claim(SCOPE, {"user": tuple(h.state()["inputs"])})
    assert step is not None
    frozen = copy.deepcopy(step["request"])
    h.send("后来输入", "u2")
    h.close()
    with Harness(tmp_path) as restored:
        restored.start()
        first = restored.provider.wait(1)
        assert [m.to_dict() for m in first.messages] == frozen["messages"]
        assert restored.provider.wait(2).messages[-1].content == "后来输入"


def test_saved_response_recovers_dispatch_without_another_model_call(tmp_path: Path) -> None:
    h = Harness(tmp_path, tools=[EchoTool("a")])
    h.send()
    h.consumers[0].consume(SCOPE)
    step = h.brain._claim(SCOPE, {"user": tuple(h.state()["inputs"])})
    assert step is not None
    response: State = {
        "content": "",
        "reasoning": None,
        "usage": {},
        "tool_calls": [{"id": "same", "name": "a", "arguments": {}}],
    }
    h.store.transact(
        SCOPE, "step.response", lambda s: s["steps"][step["step_id"]].update(response=response)
    )
    h.close()
    with Harness(tmp_path, tools=[EchoTool("a")]) as restored:
        restored.start()
        request = restored.provider.wait(1)
        # 第一次请求已经带工具结果，原始声明的模型没有重问。
        assert request.messages[-1].role == "tool"
        assert len(restored.calls()) == 1


def test_approval_and_dispatch_intent_are_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path, tools=[EchoTool("a")], policy=VerdictPolicy(default="ask"))
    h.tools.execute(SCOPE, requests("a"))
    until(lambda: h.call("a")["status"] == "waiting_approval")
    # 停止执行器，仍可提交审批事务，模拟提交成功后还没触发执行器。
    h.tools.shutdown()
    h.tools.apply_permission(SCOPE, "a", "approved", "approve")
    assert h.call("a")["status"] == "queued"
    h.close()
    with Harness(tmp_path, tools=[EchoTool("a")], policy=VerdictPolicy(default="deny")) as restored:
        restored.tools.recover_scope(SCOPE)
        until(lambda: restored.call("a")["terminal"])
        assert restored.call("a")["status"] == "succeeded"
        assert restored.call("a")["source"] == "system"
        assert len(restored.calls()) == 1


def test_restart_recovers_pending_approval_with_no_buffer(tmp_path: Path) -> None:
    h = Harness(tmp_path, tools=[EchoTool("a")], policy=VerdictPolicy(default="ask"))
    h.tools.execute(SCOPE, requests("a"))
    until(lambda: h.call("a")["status"] == "waiting_approval")
    h.close()
    with Harness(tmp_path, tools=[EchoTool("a")]) as restored:
        assert SCOPE in restored.store.scopes()
        restored.tools.recover_scope(SCOPE)
        assert restored.call("a")["status"] == "waiting_approval"
        assert not restored.call("a")["terminal"]


def test_terminal_before_notification_survives_restart(tmp_path: Path) -> None:
    h = Harness(tmp_path, tools=[EchoTool("a")])
    h.tools.execute(SCOPE, requests("a"))
    until(lambda: h.call("a")["terminal"])
    result = copy.deepcopy(h.call("a")["terminal"])
    h.queue.drain(SCOPE, "tool")
    h.close()
    with Harness(tmp_path, tools=[EchoTool("a")]) as restored:
        restored.tools.recover_scope(SCOPE)
        restored.consumers[1].consume(SCOPE)
        assert restored.buffers.snapshot(SCOPE, "tool")[0].payload == result
        assert restored.call("a")["terminal"] == result


def test_old_writer_cannot_reopen_closed_journal(tmp_path: Path) -> None:
    journal = FileJournal(tmp_path)
    store = RuntimeStore(journal)
    journal.close()
    with pytest.raises(RuntimeError, match="关闭"):
        store.transact(SCOPE, "invalid", lambda s: s["meta"].update(value=1))


@pytest.mark.slow
def test_killed_process_after_external_effect_is_not_reexecuted(tmp_path: Path) -> None:
    effect = tmp_path / "effect.txt"
    root = tmp_path / "journal"
    script = r"""
import os, sys, threading
from pathlib import Path
from tests._support.intake_fixtures import Harness, requests
from tests._support.tool_fixtures import GatedTool
from core.protocol.datatype import ToolResult

def effect(invocation):
    with open(sys.argv[2], "a") as f:
        f.write("executed\n"); f.flush(); os.fsync(f.fileno())
    print("effect committed", flush=True)
    threading.Event().wait(60)
    return ToolResult("done")
tool = GatedTool("a", body=effect)
tool.gate.set()
h = Harness(Path(sys.argv[1]), tools=[tool])
h.tools.execute(h.scope, requests("a"))
threading.Event().wait(60)
"""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(root), str(effect)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        import select

        assert process.stdout is not None  # stdout=PIPE 才有
        ready, _, _ = select.select([process.stdout], [], [], 5)
        assert ready and process.stdout.readline().strip() == "effect committed"
        process.kill()
        process.wait(timeout=5)
        with Harness(root, tools=[EchoTool("a")]) as h:
            h.tools.recover_scope(SCOPE)
            assert h.call("a")["status"] == "outcome_unknown"
            assert effect.read_text() == "executed\n"
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
