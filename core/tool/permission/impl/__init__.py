"""权限存储的实现。v1 只有 JSONL 一种。"""

from __future__ import annotations

from core.tool.permission.impl.jsonl_store import FilePermissionStore

__all__ = ["FilePermissionStore"]
