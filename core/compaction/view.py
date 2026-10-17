"""把生效的压缩产物接到历史前面。

产物落两个固定 key：`pending` 备而不用，`applied` 当前生效。一个 user 同时最多各一个 ——
历史只增不减，不需要多版本，固定 key 也就不用清理。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.compaction.boundary import is_boundary

PENDING = "pending"
APPLIED = "applied"

Artifact = Mapping[str, Any]


def artifact(state: Mapping[str, Any], key: str) -> Artifact | None:
    compactions = state.get("compactions")
    value = compactions.get(key) if isinstance(compactions, dict) else None
    return value if isinstance(value, dict) else None


def usable_artifact(
    state: Mapping[str, Any], key: str, messages: Sequence[Mapping[str, Any]]
) -> tuple[Artifact, int] | None:
    """产物和它覆盖的条数，用不了就返回 `None`。

    历史只追加，所以 `covers` 指向的位置一直存在；会失效的只有「越界」和
    「切点不再合法」两种。
    """
    value = artifact(state, key)
    if value is None or not isinstance(value.get("messages"), list):
        return None
    covers = value.get("covers")
    if not isinstance(covers, int) or not 0 < covers <= len(messages):
        return None
    return (value, covers) if is_boundary(messages, covers) else None


def compacted(messages: list[dict[str, Any]], state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """返回发给模型的视图。`messages` 是完整历史，不改动它。"""
    found = usable_artifact(state, APPLIED, messages)
    if found is None:
        return messages
    value, covers = found
    return [*value["messages"], *messages[covers:]]


__all__ = ["APPLIED", "PENDING", "Artifact", "artifact", "compacted", "usable_artifact"]
