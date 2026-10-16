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
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import QueueName
from core.queue import InMemoryEventQueue
from tests._support.intake_fixtures import SCOPE, Harness, requests, until
from tests._support.tool_fixtures import EchoTool, VerdictPolicy


def test_an_input_in_the_buffer_recovers_without_a_client(tmp_path: Path) -> None:
    """没被认领的输入住在持久 buffer 里 —— 客户端早就断开也不影响它被处理。"""
    h = Harness(tmp_path)
    h.submit(Inbound(QueueName.USER, "user.message", {"content": "已投递"}, "u1"))
    h.consumers[0].deliver(SCOPE)
    h.close()
    with Harness(tmp_path) as recovered:
        recovered.start()
        assert recovered.provider.wait(1).messages[-1].content == "已投递"


def test_claim_all_is_one_commit_and_new_input_survives_completion(tmp_path: Path) -> None:
    with Harness(tmp_path) as h:
        h.send("first")
        h.consumers[0].deliver(SCOPE)
        step = h.brain.intake.claim(SCOPE)
        assert step is not None
        h.send("second", "u2")
        h.consumers[0].deliver(SCOPE)
        h.brain.execute(SCOPE, step)
        # 这一轮跑的是 first，second 还在 buffer 里等下一轮。
        assert [i.payload["content"] for i in h.buffer.get_all(QueueName.USER)] == ["second"]
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
        h.consumers[0].deliver(SCOPE)
        original = h.journal.append

        def fail(*args: Any, **kwargs: Any) -> Any:
            if args[2].get("reason") == "step.input":
                raise OSError("磁盘故障")
            return original(*args, **kwargs)

        monkeypatch.setattr(h.journal, "append", fail)
        with pytest.raises(OSError):
            h.brain.intake.claim(SCOPE)
        # 认领是一次提交：要么整条成立，要么 journal 里什么都没有，输入退回 buffer。
        assert not h.state()["steps"]
        assert h.state()["inputs"] == {}
        assert [i.payload["content"] for i in h.buffer.get_all(QueueName.USER)] == ["你好"]


def test_claim_committed_before_reported_failure_recovers_same_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.send()
    h.consumers[0].deliver(SCOPE)
    original = h.journal.append

    def fail_after(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if args[2].get("reason") == "step.input":
            raise OSError("提交后进程中断")
        return result

    monkeypatch.setattr(h.journal, "append", fail_after)
    with pytest.raises(OSError):
        h.brain.intake.claim(SCOPE)
    saved = h.state()["meta"]["active_step"]
    h.close()
    with Harness(tmp_path) as restored:
        restored.start()
        restored.provider.wait(1)
        until(lambda: restored.state()["steps"][saved]["status"] == "completed")
        assert len(restored.state()["steps"]) == 1


def test_fixed_request_not_rebuilt_from_later_tool_or_user_state(tmp_path: Path) -> None:
    queue = InMemoryEventQueue()
    h = Harness(tmp_path, queue=queue)
    h.send("原输入")
    h.consumers[0].deliver(SCOPE)
    step = h.brain.intake.claim(SCOPE)
    assert step is not None
    step = h.brain._prepare_request(SCOPE, step)
    frozen = copy.deepcopy(step["request"])
    h.send("后来输入", "u2")
    h.close()
    with Harness(tmp_path, queue=queue) as restored:
        restored.start()
        first = restored.provider.wait(1)
        assert [m.to_dict() for m in first.messages] == frozen["messages"]
        assert restored.provider.wait(2).messages[-1].content == "后来输入"


def test_saved_response_recovers_dispatch_without_another_model_call(tmp_path: Path) -> None:
    h = Harness(tmp_path, tools=[EchoTool("a")])
    h.send()
    h.consumers[0].deliver(SCOPE)
    step = h.brain.intake.claim(SCOPE)
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


def test_a_terminal_is_still_a_fact_after_a_restart(tmp_path: Path) -> None:
    """终态在 journal 里，重启后读得回来 —— 执行过这件事不会因为通知丢了就消失。"""
    h = Harness(tmp_path, tools=[EchoTool("a")])
    h.tools.execute(SCOPE, requests("a"))
    until(lambda: h.call("a")["terminal"])
    result = copy.deepcopy(h.call("a")["terminal"])
    h.close()
    with Harness(tmp_path, tools=[EchoTool("a")]) as restored:
        restored.tools.recover_scope(SCOPE)
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
