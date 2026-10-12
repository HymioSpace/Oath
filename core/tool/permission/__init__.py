"""权限模块：判定、审批流、有效期。

权限是工具系统执行前的一道关，但不是一个布尔 —— 它有三态，还带一条要走外部
裁定的时间线。这套东西的复杂度（存储、定时器、事件）跟工具执行本身没有重叠，
所以独立成一个模块，工具系统只在接纳时问它一句。

对外两个入口：

- `PermissionManager` —— 问它能不能调（`authorize`），以及告诉它批了没（`decide`）。
- `PermissionStore` —— 审批记录的存取。换实现（MySQL）时只动 `impl/`。
"""

from __future__ import annotations

from core.tool.permission.manager import DEFAULT_TTL_SECONDS, PermissionManager
from core.tool.permission.store import PermissionStore

__all__ = ["DEFAULT_TTL_SECONDS", "PermissionManager", "PermissionStore"]
