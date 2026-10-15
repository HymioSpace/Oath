"""命令执行环境、bash 工具与工作区。

依赖方向：本模块依赖 `core/protocol` 与 `core/utils`，反过来没有。执行环境的具体实现
在 `core/shell/impl/`，装配层选一个建起来。
"""

from __future__ import annotations

from core.shell.bash_tool import NAME as BASH
from core.shell.bash_tool import BashTool, workspace_note
from core.shell.impl import HostEnvironment

__all__ = ["BASH", "BashTool", "HostEnvironment", "workspace_note"]
