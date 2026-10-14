"""技能文件：frontmatter 是索引，正文留到 `load` 再读。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

#: 技能目录里表示「这个技能」的文件名。
FILE_NAME = "SKILL.md"

#: frontmatter 的必填字段。少一个就进不了索引 —— 索引只有名字和描述。
REQUIRED = ("name", "description")


@dataclass(frozen=True, slots=True)
class Skill:
    """一条索引项。`path` 是扫描时读出来的，调用方只用 `name`。"""

    name: str
    description: str
    priority: int
    path: Path


class SkillError(ValueError):
    """这个文件不是一个技能。调用方丢掉它自己，继续扫下一个。"""


def read(path: Path) -> Skill:
    """读一个 `SKILL.md` 的 frontmatter。正文不进内存。"""
    front, _ = _split(_text(path), path)
    try:
        loaded = yaml.safe_load(front) or {}
    except yaml.YAMLError as error:
        raise SkillError(f"{path} 的 frontmatter 不是合法 YAML: {error}") from error
    if not isinstance(loaded, dict):
        raise SkillError(f"{path} 的 frontmatter 不是映射")
    fields = {name: loaded.get(name) for name in REQUIRED}
    missing = [name for name, value in fields.items() if not isinstance(value, str) or not value]
    if missing:
        raise SkillError(f"{path} 的 frontmatter 缺 {', '.join(missing)}")
    priority = loaded.get("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise SkillError(f"{path} 的 priority 必须是整数")
    return Skill(
        name=str(fields["name"]),
        description=str(fields["description"]),
        priority=priority,
        path=path,
    )


def body(path: Path) -> str:
    """读正文。frontmatter 整段去掉，包括两个 `---` 行。"""
    return _split(_text(path), path)[1]


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise SkillError(f"读不了 {path}: {error}") from error


def _split(text: str, path: Path) -> tuple[str, str]:
    """切成 (frontmatter, 正文)。没有围栏就没有 frontmatter。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError(f"{path} 没有 frontmatter 围栏")
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            return "\n".join(lines[1:end]), "\n".join(lines[end + 1 :]).lstrip()
    raise SkillError(f"{path} 的 frontmatter 没有闭合")


__all__ = ["FILE_NAME", "REQUIRED", "Skill", "SkillError", "body", "read"]
