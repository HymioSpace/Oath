"""技能索引与按需加载。

依赖方向：本模块依赖 `core/protocol` 与 `core/utils`，反过来没有。

`__init__` 只重导出机制，不转发契约层的符号 —— 契约的 import 走
`core.protocol.datatype` / `core.protocol.interface`。
"""

from __future__ import annotations

from core.skill.skill import Skill, SkillError
from core.skill.system import SkillSystem
from core.skill.tool import NAME as SKILL
from core.skill.tool import SkillTool

__all__ = ["SKILL", "Skill", "SkillError", "SkillSystem", "SkillTool"]
