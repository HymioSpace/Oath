"""技能索引：启动时扫一遍，之后只读。

正文不进索引 —— 索引只有名字、描述和优先级，正文等模型点名要了再从磁盘读。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from core.skill.skill import FILE_NAME, Skill, SkillError, body
from core.skill.skill import read as read_skill
from core.utils.tokens import estimate_text

_log = logging.getLogger(__name__)

#: 索引的抬头和使用说明。它们进预算 —— 索引装不下就整条丢掉。
_HEADER = "## 可用技能"
_HINT = '需要完整说明时调用 skill 工具：skill(action="read", name="...")。'
_LINE = "- {name}: {description}"


class SkillSystem:
    """按名字找技能。

    `skill_dirs` 是进 system prompt 末尾索引的目录，`scan_dirs` 是只能被 `find` 搜到的
    目录。两组各自按给定顺序扫，名字撞车先到先赢 —— 顺序确定，胜者就不该取决于文件
    系统返回目录项的顺序。
    """

    def __init__(self, skill_dirs: Sequence[Path], *, scan_dirs: Sequence[Path] = ()) -> None:
        self._injected: tuple[Skill, ...] = ()
        self._index: dict[str, Skill] = {}
        seen: set[Path] = set()
        sources = [(d, True) for d in skill_dirs]
        sources += [(d, False) for d in scan_dirs]
        for path, injected in sources:
            for skill in _scan(path):
                # 同一个文件被两条路径指到（符号链接）算一次：那不是撞名。
                real = skill.path.resolve()
                if real in seen:
                    continue
                seen.add(real)
                previous = self._index.get(skill.name)
                if previous is not None:
                    _log.warning(
                        "技能 %s 已在 %s 索引过，跳过 %s", skill.name, previous.path, skill.path
                    )
                    continue
                self._index[skill.name] = skill
                if injected:
                    self._injected = (*self._injected, skill)

    def list(self, budget: int) -> str:
        """进 system prompt 的索引。预算装不下就整条丢掉，不塞半截。"""
        if budget < 1:
            raise ValueError(f"索引预算必须是正整数: {budget!r}")
        lines: list[str] = []
        used = estimate_text(f"{_HEADER}\n\n{_HINT}\n")
        for skill in _by_priority(self._injected):
            line = _LINE.format(name=skill.name, description=skill.description)
            if used + estimate_text(line) > budget:
                break
            lines.append(line)
            used += estimate_text(line)
        return "" if not lines else "\n".join([_HEADER, "", _HINT, *lines])

    def find(self, query: str, *, limit: int = 5) -> Sequence[Skill]:
        """在全部技能里模糊查名字和描述，空 query 返回空列表。

        query 按空白切词，每个词都要出现在同一个技能的 name 或 description 里；
        整串命中名字比命中描述更相关，同等相关按优先级排 —— 与 `list` 的顺序一致。

        返回 `Sequence` 而不是 `list`：本类有个叫 `list` 的方法，类体里的 `list[...]`
        指的是它。
        """
        words = [word.casefold() for word in query.split() if word.strip()]
        if not words:
            return []
        whole = " ".join(words)
        found = [
            (self._relevance(skill, whole), -skill.priority, skill.name, skill)
            for skill in self._index.values()
            if all(word in self._haystack(skill) for word in words)
        ]
        found.sort(key=lambda hit: hit[:3])
        return [skill for *_, skill in found[:limit]]

    def load(self, name: str) -> str:
        """读技能正文。

        名字只在索引里查，从不拼进路径：路径是扫描时从目录项读出来的，调用方给的
        字符串只当字典键用。名字不存在抛 `KeyError`，正文读不出来抛 `SkillError` ——
        两种都让调用方当失败报，不发空串。
        """
        return body(self._index[name].path)

    @staticmethod
    def _haystack(skill: Skill) -> str:
        return f"{skill.name}\n{skill.description}".casefold()

    @staticmethod
    def _relevance(skill: Skill, whole: str) -> int:
        name = skill.name.casefold()
        if name == whole:
            return 0
        if whole in name:
            return 1
        return 2 if whole in skill.description.casefold() else 3


def _by_priority(skills: tuple[Skill, ...]) -> list[Skill]:
    """装进预算的顺序：优先级高的先放，同优先级先放短的。"""
    return sorted(skills, key=lambda s: (-s.priority, len(s.description), s.name))


def _scan(root: Path) -> list[Skill]:
    """扫一个目录下的技能子目录。目录不存在或没有技能就是空列表。"""
    if not root.is_dir():
        return []
    skills = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        path = entry / FILE_NAME
        if not entry.is_dir() or not path.is_file():
            continue
        try:
            skills.append(read_skill(path))
        except SkillError as error:
            _log.warning("跳过技能: %s", error)
    return skills


__all__ = ["SkillSystem"]
