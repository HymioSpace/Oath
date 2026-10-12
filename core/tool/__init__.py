"""工具系统的机制层：注册表与执行后端。

契约在 `core/protocol/`（`Tool` / `ToolSystem` / `ToolEventSink` 与相关数据类型），
执行后端在 `core/tool/impl/`，执行前那道权限关在 `core/tool/permission/`。

依赖方向：本模块依赖 `core/protocol`，反过来没有。

`__init__` 只重导出机制，不转发契约层的符号 —— 契约的 import 走
`core.protocol.datatype` / `core.protocol.interface`。
"""

from __future__ import annotations

from core.tool.registry import DEFAULT_TIMEOUT_SECONDS, RegisteredTool, ToolRegistry

__all__ = ["DEFAULT_TIMEOUT_SECONDS", "RegisteredTool", "ToolRegistry"]
