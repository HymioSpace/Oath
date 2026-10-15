"""技能索引：frontmatter 只进索引，正文按需读，坏文件只丢自己。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.protocol.datatype import Scope, ToolInvocation
from core.protocol.error import ToolExecutionError
from core.skill import SKILL, SkillSystem, SkillTool
from core.utils.tokens import estimate_text

SCOPE = Scope("u1", "s1")


def write(root: Path, name: str, *, description: str, priority: int | None = None) -> Path:
    """放一个技能进去。`priority` 为 `None` 时不写这个字段。"""
    entry = root / name
    entry.mkdir(parents=True)
    head = f"name: {name}\ndescription: {description}\n"
    if priority is not None:
        head += f"priority: {priority}\n"
    path = entry / "SKILL.md"
    path.write_text(f"---\n{head}---\n# {name}\n\n{name} 的正文。\n", encoding="utf-8")
    return path


def skills(tmp_path: Path) -> SkillSystem:
    return SkillSystem([tmp_path / "skills"], scan_dirs=[tmp_path / "external"])


def test_only_the_frontmatter_reaches_the_index(tmp_path: Path) -> None:
    write(tmp_path / "skills", "pdf", description="填写 PDF 表单。", priority=3)

    index = skills(tmp_path)

    assert "pdf: 填写 PDF 表单。" in index.list(500)
    assert "正文" not in index.list(500)
    assert index.load("pdf").startswith("# pdf")


def test_scanned_skills_are_findable_but_not_prompted(tmp_path: Path) -> None:
    write(tmp_path / "skills", "mine", description="项目技能。")
    write(tmp_path / "external", "theirs", description="外部技能。")

    index = skills(tmp_path)

    assert "theirs" not in index.list(500)
    assert [hit.name for hit in index.find("外部")] == ["theirs"]
    assert index.load("theirs").startswith("# theirs")


def test_the_budget_drops_the_lowest_priority_first(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    write(root, "high", description="优先级高。", priority=10)
    write(root, "mid", description="优先级中。", priority=5)
    write(root, "low", description="优先级低。", priority=1)

    index = skills(tmp_path)
    full = index.list(5000)
    lines = [line for line in full.splitlines() if line.startswith("- ")]
    # 抬头和使用说明的固定开销，加上刚好一条的额度。
    chrome = estimate_text(full) - sum(estimate_text(line) for line in lines)

    prompt = index.list(chrome + estimate_text(lines[0]))

    assert [line for line in prompt.splitlines() if line.startswith("- ")] == [lines[0]]
    assert [hit.name for hit in index.find("优先级")] == ["high", "mid", "low"]


def test_an_index_that_cannot_fit_even_one_skill_is_dropped(tmp_path: Path) -> None:
    write(tmp_path / "skills", "pdf", description="填写 PDF 表单。")

    assert skills(tmp_path).list(10) == ""


def test_the_same_priority_shorter_descriptions_come_first(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    write(root, "long", description="短" * 200)
    write(root, "short", description="短")

    index = skills(tmp_path)
    full = index.list(5000)

    assert full.index("short") < full.index("long")


def test_the_first_directory_scanned_wins_a_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write(tmp_path / "skills", "dup", description="项目那份。")
    write(tmp_path / "external", "dup", description="外部那份。")

    index = skills(tmp_path)

    assert "项目那份。" in index.list(500)
    assert index.load("dup").startswith("# dup")
    assert "跳过" in caplog.text


def test_the_same_file_behind_two_paths_is_indexed_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write(tmp_path / "external", "linked", description="同一个文件。")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "linked").symlink_to(tmp_path / "external" / "linked")

    index = skills(tmp_path)

    assert [hit.name for hit in index.find("同一个")] == ["linked"]
    assert "已索引过" not in caplog.text
    assert index.load("linked").startswith("# linked")


@pytest.mark.parametrize(
    "content",
    [
        "没有围栏",
        "---\nname: a\n",  # 围栏没闭合
        "---\ndescription: 只有描述。\n---\n正文",
        "---\nname: a\ndescription: ''\n---\n正文",
        "---\nname: a\ndescription: 描述。\npriority: 高\n---\n正文",
        "---\n- 这不是映射\n---\n正文",
        "---\nname: [a\n---\n正文",  # 非法 YAML
    ],
)
def test_a_broken_skill_is_dropped_without_taking_the_others(tmp_path: Path, content: str) -> None:
    broken = tmp_path / "skills" / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text(content, encoding="utf-8")
    write(tmp_path / "skills", "good", description="好的那个。")

    index = skills(tmp_path)

    assert [hit.name for hit in index.find("")] == []
    assert index.list(500).count("- ") == 1
    assert [hit.name for hit in index.find("好的")] == ["good"]


def test_a_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    index = SkillSystem([tmp_path / "nowhere"], scan_dirs=[tmp_path / "also-nowhere"])

    assert index.list(500) == ""
    assert index.find("任何") == []
    with pytest.raises(KeyError):
        index.load("任何")


def test_find_needs_every_word_but_reads_one_of_them(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    write(root, "pdf-fill", description="填写 PDF 表单字段。")
    write(root, "pdf-preview", description="预览 PDF。")

    index = skills(tmp_path)

    assert [hit.name for hit in index.find("PDF 表单")] == ["pdf-fill"]
    assert index.find("pdf excel") == []
    assert index.find("  ") == []


def test_a_name_that_looks_like_a_path_is_only_a_dictionary_key(tmp_path: Path) -> None:
    write(tmp_path / "skills", "pdf", description="填写 PDF 表单。")

    index = skills(tmp_path)

    with pytest.raises(KeyError):
        index.load("../../etc/passwd")
    assert index.find("../../etc") == []


def test_the_tool_reads_back_what_the_index_omitted(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    write(root, "pdf", description="填写 PDF 表单。")
    write(root, "other", description="别的。")
    index = skills(tmp_path)
    tool = SkillTool(index)

    found = tool.execute(ToolInvocation(SCOPE, "i1", {"action": "search", "query": "PDF"}, None))
    read = tool.execute(ToolInvocation(SCOPE, "i2", {"action": "read", "name": "pdf"}, None))
    empty = tool.execute(ToolInvocation(SCOPE, "i3", {"action": "search", "query": "xlsx"}, None))

    assert found.content == "- pdf: 填写 PDF 表单。"
    assert read.content == index.load("pdf")
    assert "没有匹配" in str(empty.content)
    assert tool.definition.name == SKILL


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "search"},  # 缺 query
        {"action": "read", "name": ""},  # 空 name
        {"action": "write", "name": "pdf"},
        {"name": "pdf"},
        {"action": "search", "query": 3},
    ],
)
def test_bad_arguments_fail_the_tool_instead_of_guessing(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    write(tmp_path / "skills", "pdf", description="填写 PDF 表单。")

    with pytest.raises(ToolExecutionError):
        SkillTool(skills(tmp_path)).execute(ToolInvocation(SCOPE, "i1", arguments, None))


def test_reading_an_unindexed_skill_fails_the_tool(tmp_path: Path) -> None:
    write(tmp_path / "skills", "pdf", description="填写 PDF 表单。")

    with pytest.raises(ToolExecutionError):
        SkillTool(skills(tmp_path)).execute(
            ToolInvocation(SCOPE, "i1", {"action": "read", "name": "nope"}, None)
        )
