"""压缩接进执行链：发给模型的内容被压过、被截过，取回全文仍然可行。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from core.journal.runtime_store import State
from core.protocol.datatype import ToolDefinition, ToolInvocation, ToolResult
from core.protocol.datatype.config import AgentConfig
from core.protocol.datatype.llm import LlmRequest, LlmResponse, LlmToolCall
from core.protocol.interface.compaction import Compactor
from core.protocol.interface.tool import Tool
from tests._support.intake_fixtures import Harness, until

KEY = re.compile(r"detail_key=([\w.-]+)\]")


class LongTextTool(Tool):
    """每次都返回一段固定的长文本。"""

    def __init__(self, body: str = "结果" * 250) -> None:
        self.body = body
        self._definition = ToolDefinition(
            name="long_text",
            description="返回长文本",
            input_schema={"type": "object", "properties": {}},
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def execute(self, invocation: ToolInvocation) -> ToolResult:
        return ToolResult(content=self.body)


class DigestCompactor(Compactor):
    def __init__(self, digest: str = "<context_summary>摘要</context_summary>") -> None:
        self.digest = digest
        self.calls = 0

    def compress(
        self, messages: Sequence[Mapping[str, Any]], *, budget_tokens: int
    ) -> list[dict[str, Any]] | None:
        self.calls += 1
        return [{"role": "user", "content": self.digest}]


def tool_messages(request: LlmRequest) -> list[str]:
    return [m.content for m in request.messages if m.role == "tool"]


def detail_key_of(request: LlmRequest) -> str:
    match = KEY.search(tool_messages(request)[-1])
    assert match is not None, "结果里没有截断说明"
    return match.group(1)


# --------------------------------------------------------------------------
# 工具结果截断
# --------------------------------------------------------------------------


def test_a_long_result_reaches_the_model_truncated(tmp_path: Path) -> None:
    config = AgentConfig(
        tool_round_timeout_seconds=0.5, llm_retry_delay=0.01, tool_result_detail_chars=100
    )
    with Harness(
        tmp_path,
        tools=[LongTextTool()],
        config=config,
        responses=[
            LlmResponse(tool_calls=(LlmToolCall("long_text", {}, "c1"),)),
            LlmResponse(content="读完了"),
        ],
    ) as harness:
        harness.start().send()
        harness.provider.wait(2)

        [seen] = tool_messages(harness.provider.wait(2))
        assert len(seen) < 200
        assert "已省略" in seen
        assert harness.call("long_text")["terminal"]["original_chars"] == 500


def test_the_model_can_read_the_truncated_part_back(tmp_path: Path) -> None:
    config = AgentConfig(
        tool_round_timeout_seconds=0.5, llm_retry_delay=0.01, tool_result_detail_chars=100
    )
    with Harness(
        tmp_path,
        tools=[LongTextTool()],
        config=config,
        responses=[
            LlmResponse(tool_calls=(LlmToolCall("long_text", {}, "c1"),)),
            lambda request: LlmResponse(
                tool_calls=(
                    LlmToolCall("detail_read", {"detail_key": detail_key_of(request)}, "c2"),
                )
            ),
            LlmResponse(content="读完了"),
        ],
    ) as harness:
        harness.start().send()
        harness.provider.wait(3)
        until(lambda: harness.state()["meta"].get("active_step") is None)

        # detail_read 自己的结果不再截断。
        assert tool_messages(harness.provider.wait(3))[-1] == "结果" * 250


def test_the_terminal_payload_stays_valid_public_contract(tmp_path: Path) -> None:
    from core.journal.validation import validate_payload
    from core.protocol.datatype.journal import EventType

    config = AgentConfig(
        tool_round_timeout_seconds=0.5, llm_retry_delay=0.01, tool_result_detail_chars=100
    )
    with Harness(tmp_path, tools=[LongTextTool()], config=config) as harness:
        harness.tools.execute(harness.scope, _requests("long_text"))
        until(lambda: harness.call("long_text")["terminal"])

        validate_payload(EventType.TOOL_RESULT, 1, harness.call("long_text")["terminal"])


def _requests(name: str) -> Any:
    from core.protocol.datatype import ToolCallRequest

    return [ToolCallRequest(name, {}, f"provider-{name}", name, "b1", "r1")]


# --------------------------------------------------------------------------
# 历史压缩
# --------------------------------------------------------------------------


def test_the_compressed_view_is_what_the_model_sees(tmp_path: Path) -> None:
    config = AgentConfig(
        tool_round_timeout_seconds=0.5,
        llm_retry_delay=0.01,
        context_window=200,
        compress_async_at_ratio=0.4,
        compress_sync_at_ratio=0.9,
        compress_keep_recent=2,
        compress_summary_tokens=1,
    )
    with Harness(tmp_path, config=config, compactor=DigestCompactor()) as harness:
        history: list[State] = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": "字" * 60}
            for index in range(6)
        ]
        harness.store.transact(
            harness.scope, "conversation.seed", lambda s: s["conversation"].update(messages=history)
        )
        harness.start().send("继续")

        request = harness.provider.wait(1)
        assert [m.content for m in request.messages] == [
            "<context_summary>摘要</context_summary>",
            "字" * 60,
            "继续",
        ]
        # 历史本身一条不少，压缩只改发给模型的那一份。
        assert len(harness.state()["conversation"]["messages"]) == 7
