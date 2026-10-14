"""历史压缩：切点、阈值、生效，以及压缩结果确实进了下一次请求。"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from core.compaction import CompactionCoordinator, LlmCompactor, cut_index, estimate
from core.compaction.llm import transcript
from core.compaction.tokens import estimate_text
from core.compaction.view import APPLIED, PENDING
from core.journal.impl import FileJournal
from core.journal.runtime_store import RuntimeStore, State
from core.protocol.datatype.config import AgentConfig
from core.protocol.datatype.llm import LlmRequest, LlmResponse
from core.protocol.datatype.scope import Scope
from core.protocol.interface.compaction import Compactor
from core.protocol.interface.llm import DeltaSink, ModelProvider
from tests._support.intake_fixtures import until

SCOPE = Scope("u1", "s1")

#: 一条消息 60 个汉字约 60 token。窗口 500 时：6 条（360）过异步阈值不过同步阈值，
#: 10 条（600）两级都过。
WINDOW = 500


def config(*, window: int = WINDOW, async_ratio: float = 0.4, sync: float = 0.9) -> AgentConfig:
    return AgentConfig(
        context_window=window,
        compress_async_at_ratio=async_ratio,
        compress_sync_at_ratio=sync,
        compress_keep_recent=2,
        compress_summary_tokens=1,
    )


class CountingCompactor(Compactor):
    """返回固定摘要，记下被问了几次。"""

    def __init__(self, digest: str = "摘要") -> None:
        self.digest = digest
        self.calls = 0
        self.segments: list[list[Mapping[str, Any]]] = []
        self._lock = threading.Lock()

    def compress(
        self, messages: Sequence[Mapping[str, Any]], *, budget_tokens: int
    ) -> list[dict[str, Any]] | None:
        with self._lock:
            self.calls += 1
            self.segments.append(list(messages))
        return [{"role": "user", "content": self.digest}]


class GatedCompactor(CountingCompactor):
    """卡在 `gate` 上，用来观察「同时只跑一个」。"""

    def __init__(self, gate: threading.Event) -> None:
        super().__init__()
        self.gate = gate

    def compress(
        self, messages: Sequence[Mapping[str, Any]], *, budget_tokens: int
    ) -> list[dict[str, Any]] | None:
        self.gate.wait(timeout=5)
        return super().compress(messages, budget_tokens=budget_tokens)


def history(count: int, size: int = 60) -> list[State]:
    """`count` 条交替的 user / assistant，每条都够大到撑破阈值。"""
    return [
        {"role": "user" if index % 2 == 0 else "assistant", "content": "字" * size}
        for index in range(count)
    ]


def build(
    tmp_path: Path, compactor: Compactor, settings: AgentConfig | None = None
) -> tuple[FileJournal, RuntimeStore, CompactionCoordinator]:
    journal = FileJournal(tmp_path / "journal")
    store = RuntimeStore(journal)
    return journal, store, CompactionCoordinator(store, compactor, settings or config())


def seed(store: RuntimeStore, messages: list[State]) -> None:
    def change(state: State) -> None:
        state["conversation"]["messages"] = messages

    store.transact(SCOPE, "conversation.seed", change)


def extend(store: RuntimeStore, messages: list[State]) -> None:
    """接着对话往下写。历史只增不减。"""

    def change(state: State) -> None:
        state["conversation"]["messages"].extend(messages)

    store.transact(SCOPE, "conversation.extended", change)


# --------------------------------------------------------------------------
# 切点
# --------------------------------------------------------------------------


def test_the_cut_never_lands_inside_a_tool_group() -> None:
    messages: list[State] = [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "content": "结果 A", "tool_call_id": "a"},
        {"role": "assistant", "content": "回答"},
    ]

    # 声明之前是合法切点；结果内部的切点退回声明之前。
    assert cut_index(messages, 3) == 1
    assert cut_index(messages, 2) == 1


def test_the_cut_is_the_first_legal_boundary_from_the_tail() -> None:
    messages: list[State] = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "content": "结果", "tool_call_id": "a"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "b"}]},
        {"role": "tool", "content": "结果", "tool_call_id": "b"},
        {"role": "user", "content": "继续"},
    ]

    assert cut_index(messages, 3) == 2


def test_short_history_is_not_compressible() -> None:
    assert cut_index(history(4), 4) is None
    assert cut_index(history(4), 2) == 2


# --------------------------------------------------------------------------
# 计量
# --------------------------------------------------------------------------


def test_costs_more_than_latin_text_of_the_same_length() -> None:
    assert estimate_text("中" * 100) > estimate_text("a" * 100)


def test_tool_call_arguments_are_counted() -> None:
    plain = estimate([{"role": "user", "content": "x"}])

    assert (
        estimate([{"role": "assistant", "tool_calls": [{"name": "t", "arguments": "x" * 400}]}])
        > plain
    )


# --------------------------------------------------------------------------
# 阈值与生效
# --------------------------------------------------------------------------


def test_nothing_happens_below_the_async_threshold(tmp_path: Path) -> None:
    journal, store, coordinator = build(tmp_path, CountingCompactor(), config(window=100_000))
    seed(store, history(6))
    try:
        coordinator.prepare(SCOPE)

        assert store.read(SCOPE)["compactions"] == {}
    finally:
        journal.close()


def test_async_compression_is_stored_but_not_applied(tmp_path: Path) -> None:
    journal, store, coordinator = build(tmp_path, CountingCompactor())
    seed(store, history(6))
    try:
        coordinator.prepare(SCOPE)
        until(lambda: store.read(SCOPE)["compactions"].get(PENDING))

        state = store.read(SCOPE)
        assert APPLIED not in state["compactions"]
        assert state["compactions"][PENDING]["covers"] == 4
        assert coordinator.view(history(6), state) == history(6)
    finally:
        journal.close()


def test_the_pending_artifact_does_not_shrink_the_request(tmp_path: Path) -> None:
    journal, store, coordinator = build(tmp_path, CountingCompactor())
    seed(store, history(6))
    try:
        coordinator.prepare(SCOPE)
        until(lambda: store.read(SCOPE)["compactions"].get(PENDING))

        assert [m["content"] for m in coordinator.view(history(6), store.read(SCOPE))] == [
            "字" * 60
        ] * 6
    finally:
        journal.close()


def test_sync_threshold_applies_the_prepared_artifact(tmp_path: Path) -> None:
    compactor = CountingCompactor()
    journal, store, coordinator = build(tmp_path, compactor)
    seed(store, history(6))
    try:
        coordinator.prepare(SCOPE)
        until(lambda: store.read(SCOPE)["compactions"].get(PENDING))
        prepared = store.read(SCOPE)["compactions"][PENDING]
        extend(store, history(2))

        coordinator.prepare(SCOPE)

        state = store.read(SCOPE)
        # 生效的就是异步备好的那一份，同步路径没有再压一次。
        assert state["compactions"][APPLIED] == prepared
        view = coordinator.view(history(8), state)
        assert view[0] == {"role": "user", "content": "摘要"}
        assert view[1:] == history(8)[4:]
    finally:
        journal.close()


def test_sync_threshold_compresses_when_nothing_is_prepared(tmp_path: Path) -> None:
    compactor = CountingCompactor()
    journal, store, coordinator = build(tmp_path, compactor)
    seed(store, history(10))
    try:
        coordinator.prepare(SCOPE)

        assert compactor.calls == 1
        assert store.read(SCOPE)["compactions"][APPLIED]["covers"] == 8
        assert len(coordinator.view(history(10), store.read(SCOPE))) == 3
    finally:
        journal.close()


def test_one_async_compression_per_scope(tmp_path: Path) -> None:
    gate = threading.Event()
    compactor = GatedCompactor(gate)
    journal, store, coordinator = build(tmp_path, compactor)
    seed(store, history(6))
    try:
        coordinator.prepare(SCOPE)
        coordinator.prepare(SCOPE)
        coordinator.prepare(SCOPE)

        gate.set()
        until(lambda: store.read(SCOPE)["compactions"].get(PENDING))
        assert compactor.calls == 1
    finally:
        journal.close()


def test_a_pending_artifact_covering_less_is_never_reapplied(tmp_path: Path) -> None:
    """生效之后不许退回更少的历史。"""
    journal, store, coordinator = build(tmp_path, CountingCompactor())
    seed(store, history(10))
    coordinator.prepare(SCOPE)

    def stale(state: State) -> None:
        state["compactions"][PENDING] = {
            "covers": 3,
            "messages": [{"role": "user", "content": "更少"}],
        }

    store.transact(SCOPE, "context.compressed", stale)
    try:
        coordinator.prepare(SCOPE)

        state = store.read(SCOPE)
        assert state["compactions"][APPLIED]["covers"] == 8
        assert coordinator.view(history(10), state)[0]["content"] == "摘要"
    finally:
        journal.close()


def test_the_applied_artifact_survives_a_reload(tmp_path: Path) -> None:
    journal, store, coordinator = build(tmp_path, CountingCompactor())
    seed(store, history(10))
    coordinator.prepare(SCOPE)
    reopened = RuntimeStore(journal)
    try:
        state = reopened.read(SCOPE)
        assert state["compactions"][APPLIED]["covers"] == 8
        assert coordinator.view(history(10), state)[0]["content"] == "摘要"
    finally:
        journal.close()


def test_a_failing_compressor_keeps_the_history(tmp_path: Path) -> None:
    class Broken(Compactor):
        def compress(
            self, messages: Sequence[Mapping[str, Any]], *, budget_tokens: int
        ) -> list[dict[str, Any]] | None:
            raise RuntimeError("压缩服务不可用（测试）")

    journal, store, coordinator = build(tmp_path, Broken())
    seed(store, history(10))
    try:
        coordinator.prepare(SCOPE)

        assert store.read(SCOPE)["compactions"] == {}
    finally:
        journal.close()


def test_a_summary_longer_than_the_history_is_refused(tmp_path: Path) -> None:
    journal, store, coordinator = build(tmp_path, CountingCompactor("摘要" * 400))
    seed(store, history(4))
    try:
        coordinator.prepare(SCOPE)

        assert store.read(SCOPE)["compactions"] == {}
    finally:
        journal.close()


def test_an_artifact_out_of_range_is_ignored(tmp_path: Path) -> None:
    journal, store, coordinator = build(tmp_path, CountingCompactor())

    def apply(state: State) -> None:
        state["conversation"]["messages"] = history(4)
        state["compactions"][APPLIED] = {
            "covers": 99,
            "messages": [{"role": "user", "content": "摘要"}],
        }

    store.transact(SCOPE, "context.compression_applied", apply)
    try:
        assert coordinator.view(history(4), store.read(SCOPE)) == history(4)
    finally:
        journal.close()


# --------------------------------------------------------------------------
# LLM 实现
# --------------------------------------------------------------------------


class StaticProvider(ModelProvider):
    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list[LlmRequest] = []

    def complete(self, request: LlmRequest, *, on_delta: DeltaSink | None = None) -> LlmResponse:
        self.requests.append(request)
        return LlmResponse(content=self.content)


def test_the_xml_contract_becomes_one_user_message() -> None:
    provider = StaticProvider(
        "<context_digest><summary>改了地址 &amp; 还没发货</summary>"
        "<facts><item>订单 1234</item></facts>"
        "<open><item>还没回答价格</item></open></context_digest>"
    )
    compactor = LlmCompactor(provider, model="m", max_output_tokens=2048)

    result = compactor.compress(history(4), budget_tokens=100)

    assert result is not None
    assert len(result) == 1
    assert result[0]["role"] == "user"
    assert "改了地址 &amp; 还没发货" in result[0]["content"]
    assert "订单 1234" in result[0]["content"]
    assert "还没回答价格" in result[0]["content"]


def test_a_fenced_answer_still_parses() -> None:
    provider = StaticProvider(
        "```xml\n<context_digest><summary>只有摘要</summary></context_digest>\n```"
    )
    compactor = LlmCompactor(provider, model="m", max_output_tokens=2048)

    result = compactor.compress(history(4), budget_tokens=100)

    assert result is not None and "只有摘要" in result[0]["content"]


def test_output_that_is_not_the_contract_is_refused() -> None:
    for content in ("我看了一下这些历史", "<context_digest><summary></summary></context_digest>"):
        assert (
            LlmCompactor(StaticProvider(content), model="m", max_output_tokens=2048).compress(
                history(4), budget_tokens=100
            )
            is None
        )


def test_the_transcript_keeps_the_original_index() -> None:
    messages: list[State] = [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "", "tool_calls": [{"name": "t", "arguments": "{}"}]},
    ]

    assert transcript(messages) == "[1] user: 问题\n[2] assistant: \n[2] assistant tool_calls: t {}"
