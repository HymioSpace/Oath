"""权限策略与原子审批；持久化使用 core 的共用 RuntimeStore。"""

from core.tool.permission.manager import DEFAULT_TTL_SECONDS, PermissionManager

__all__ = ["DEFAULT_TTL_SECONDS", "PermissionManager"]
