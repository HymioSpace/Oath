"""压缩的保留边界、请求准备和恢复。"""

import copy
import os
import stat
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from core.compaction import cut_index
from core.compaction.boundary import is_boundary
from core.context.intake import validate_messages
from core.detail.impl import FileDetailStore
from core.detail.shrink import ResultShrinker
from core.journal.runtime_store import State
from core.protocol.datatype.config import AgentConfig
from core.protocol.datatype.llm import LlmRequest
from core.protocol.datatype.scope import Scope
from core.queue import InMemoryEventQueue
from core.utils.encoding import canonical_json
from core.utils.tokens import estimate, estimate_text
from tests._support.intake_fixtures import Harness, until
from tests.test_context_compaction import CountingCompactor, build, extend, history, seed

SCOPE = Scope("u1", "s1")


def conversation_of(request: LlmRequest) -> list[str]:
    """请求里除 system prompt 之外的消息。压缩动的是这一段。"""
    return [m.content for m in request.messages if m.role != "system"]


def test_every_cut_preserves_at_least_n_and_complete_tool_groups() -> None:
    messages: list[State] = [{"role": "user", "content": "start"}]
    for index in range(5):
        calls = [{"id": f"{index}-{j}", "name": "test", "arguments": {}} for j in range(3)]
        messages.append({"role": "assistant", "content": "", "tool_calls": calls})
        messages.extend({"role": "tool", "tool_call_id": c["id"], "content": "ok"} for c in calls)
        messages.append({"role": "assistant", "content": "done"})
    for keep in range(1, len(messages) + 1):
        cut = cut_index(messages, keep)
        if cut is not None:
            assert len(messages) - cut >= keep
            validate_messages(messages[:cut])
            validate_messages(messages[cut:])
    assert not is_boundary(messages[:2], 2)  # 尚未收到 result 的末尾不是合法边界。


def test_current_input_and_request_overhead_trigger_compression(tmp_path: Path) -> None:
    config = AgentConfig(
        context_window=3000,
        max_output_tokens=100,
        compress_keep_recent=2,
        compress_summary_tokens=10,
    )
    compactor = CountingCompactor()
    with Harness(tmp_path, config=config, compactor=compactor) as h:
        seed(h.store, history(6, 180))
        h.start().send("新" * 1800)
        request = h.provider.wait(1)
        assert compactor.calls == 1
        assert conversation_of(request)[0] == "摘要"
        size = estimate([m.to_dict() for m in request.messages])
        size += estimate_text(canonical_json([t.to_dict() for t in request.tools]))
        assert size + request.max_output_tokens < config.context_window
        assert request.messages[-1].content == "新" * 1800


def test_output_reservation_alone_can_cross_sync_threshold(tmp_path: Path) -> None:
    config = AgentConfig(
        context_window=1000,
        max_output_tokens=600,
        compress_keep_recent=2,
        compress_summary_tokens=1,
    )
    with Harness(tmp_path, config=config, compactor=CountingCompactor()) as h:
        seed(h.store, history(6, 60))
        h.start().send("go")
        assert conversation_of(h.provider.wait(1))[0] == "摘要"


def test_second_summary_uses_applied_digest_and_only_uncovered_history(tmp_path: Path) -> None:
    compactor = CountingCompactor()
    journal, store, coordinator = build(tmp_path, compactor)
    try:
        seed(store, history(10))
        coordinator.prepare(SCOPE)
        assert compactor.calls == 1
        first_cut = cast(dict[str, Any], store.read(SCOPE)["compactions"]["applied"])["covers"]
        extend(store, history(8))
        coordinator.prepare(SCOPE)
        assert compactor.calls == 2
        second = compactor.segments[1]
        assert second[0]["content"] == "摘要"
        assert len(second) == 1 + 18 - 2 - first_cut
    finally:
        coordinator.shutdown()
        journal.close()


def test_stale_wake_and_model_retry_do_not_prepare_context_again(tmp_path: Path) -> None:
    with (
        Harness(tmp_path, responses=[RuntimeError("retry")]) as h,
        patch.object(h.compaction, "prepare", wraps=h.compaction.prepare) as prepare,
    ):
        h.start()
        h.brain.wake(h.scope)
        h.send()
        h.provider.wait(2)
        until(lambda: h.state()["meta"].get("active_step") is None)
        assert prepare.call_count == 1
        assert h.provider.requests[0] == h.provider.requests[1]


def test_restart_between_input_claim_and_request_freeze_keeps_input(tmp_path: Path) -> None:
    queue = InMemoryEventQueue()
    h = Harness(tmp_path, queue=queue)
    h.send("original")
    h.consumers[0].deliver(h.scope)
    step = h.brain.intake.claim(h.scope)
    assert step is not None
    assert step["request"] is None
    draft = copy.deepcopy(step["request_draft"])
    h.send("later", "later")
    h.close()
    # 队列在进程外：重启之后那条没被认领的输入还在队列里等着。
    with Harness(tmp_path, queue=queue) as restored:
        restored.start()
        request = restored.provider.wait(1)
        assert [m.to_dict() for m in request.messages] == draft["messages"]
        assert restored.provider.wait(2).messages[-1].content == "later"


def test_detail_rename_is_followed_by_directory_fsync(tmp_path: Path) -> None:
    events: list[str] = []
    original_sync, original_replace = os.fsync, os.replace

    def sync(fd: int) -> None:
        events.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        original_sync(fd)

    def replace(*args: Any) -> None:
        original_replace(*args)
        events.append("rename")

    store = FileDetailStore(tmp_path / "details")
    with patch("os.fsync", side_effect=sync), patch("os.replace", side_effect=replace):
        store.put(Scope("u1", "s1"), "result", "full text")
    assert events[-3:] == ["file", "rename", "dir"]
    assert store.get(Scope("u1", "s1"), "result") == "full text"


def test_directory_sync_failure_retains_original_result(tmp_path: Path) -> None:
    store = FileDetailStore(tmp_path)
    scope = Scope("u1", "s1")
    store.put(scope, "initial", "init")
    shrinker = ResultShrinker(store, 4)
    with patch("core.detail.impl.file_detail_store._sync_directory", side_effect=OSError("disk")):
        content, detail = shrinker.shrink("test", scope, "result", "complete original")
    assert content == "complete original"
    assert detail is None
