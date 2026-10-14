"""技能接进执行链：索引进 system prompt，正文靠模型点名取回。"""

from __future__ import annotations

from pathlib import Path

from core.protocol.datatype.config import AgentConfig
from core.protocol.datatype.llm import LlmRequest, LlmResponse, LlmToolCall
from tests._support.intake_fixtures import Harness, until
from tests.test_skill_index import write

#: 截断阈值调到很小，skill 的返回值才可能看起来像是被截断了。
CONFIG = AgentConfig(
    system_prompt="你是助手",
    tool_result_detail_chars=80,
    tool_round_timeout_seconds=0.5,
    llm_retry_delay=0.01,
)


def tool_results(request: LlmRequest) -> list[str]:
    return [m.content for m in request.messages if m.role == "tool"]


def system_prompt_of(request: LlmRequest) -> str:
    return next(m.content for m in request.messages if m.role == "system")


def test_the_index_lands_at_the_end_of_the_system_prompt(tmp_path: Path) -> None:
    write(tmp_path / "skills", "pdf", description="填写 PDF 表单。", priority=5)
    write(tmp_path / "external-skills", "theirs", description="外部技能。")

    with Harness(tmp_path, config=CONFIG) as harness:
        harness.start().send()
        prompt = system_prompt_of(harness.provider.wait(1))

    assert prompt.startswith("你是助手")
    assert "- pdf: 填写 PDF 表单。" in prompt
    assert "theirs" not in prompt


def test_an_empty_index_leaves_the_system_prompt_untouched(tmp_path: Path) -> None:
    with Harness(tmp_path, config=CONFIG) as harness:
        harness.start().send()

        assert system_prompt_of(harness.provider.wait(1)) == "你是助手"


def test_a_scanned_skill_is_reachable_by_name_but_never_stated_up_front(tmp_path: Path) -> None:
    write(tmp_path / "external-skills", "theirs", description="外部技能。")

    with Harness(
        tmp_path,
        config=CONFIG,
        responses=[
            LlmResponse(
                tool_calls=(LlmToolCall("skill", {"action": "search", "query": "外部"}, "c1"),)
            ),
            LlmResponse(
                tool_calls=(LlmToolCall("skill", {"action": "read", "name": "theirs"}, "c2"),)
            ),
            LlmResponse(content="读完了"),
        ],
    ) as harness:
        harness.start().send()
        harness.provider.wait(3)
        until(lambda: harness.state()["meta"].get("active_step") is None)

        searched, read = tool_results(harness.provider.wait(3))
        assert searched == "- theirs: 外部技能。"
        assert read == harness.skills.load("theirs")


def test_a_long_skill_body_reaches_the_model_whole(tmp_path: Path) -> None:
    write(tmp_path / "skills", "long", description="一个长技能。")
    (tmp_path / "skills" / "long" / "SKILL.md").write_text(
        "---\nname: long\ndescription: 一个长技能。\n---\n" + "正文很长。" * 200, encoding="utf-8"
    )

    with Harness(
        tmp_path,
        config=CONFIG,
        responses=[
            LlmResponse(
                tool_calls=(LlmToolCall("skill", {"action": "read", "name": "long"}, "c1"),)
            ),
            LlmResponse(content="读完了"),
        ],
    ) as harness:
        harness.start().send()
        harness.provider.wait(2)
        until(lambda: harness.state()["meta"].get("active_step") is None)

        [seen] = tool_results(harness.provider.wait(2))
        assert seen == "正文很长。" * 200
        assert "已省略" not in seen


def test_the_skill_tool_is_registered_and_declared_to_the_model(tmp_path: Path) -> None:
    with Harness(tmp_path, config=CONFIG) as harness:
        harness.start().send()
        request = harness.provider.wait(1)

    assert {t.name for t in request.tools} >= {"skill", "detail_read"}
    assert harness.call("skill") is None
