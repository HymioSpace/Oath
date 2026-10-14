"""按完整 tool call / result 组寻找保留区间。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def is_boundary(messages: Sequence[Mapping[str, Any]], index: int) -> bool:
    """切点前没有待配对的 tool call，切点后不以孤立 result 开始。"""
    if not 0 <= index <= len(messages):
        return False
    pending: list[str] = []
    for message in messages[:index]:
        if pending:
            if message.get("role") != "tool" or message.get("tool_call_id") != pending.pop(0):
                return False
        elif message.get("role") == "tool":
            return False
        calls = message.get("tool_calls") or []
        if calls:
            if message.get("role") != "assistant":
                return False
            pending = [call.get("id") for call in calls]
            if any(not key for key in pending) or len(set(pending)) != len(pending):
                return False
    return not pending and (index == len(messages) or messages[index].get("role") != "tool")


def cut_index(messages: Sequence[Mapping[str, Any]], keep_recent: int) -> int | None:
    """从 len-N 向更早的合法边界移动，至少保留 N 条。"""
    if isinstance(keep_recent, bool) or not isinstance(keep_recent, int) or keep_recent < 1:
        raise ValueError(f"保留条数必须是正整数: {keep_recent!r}")
    index = len(messages) - keep_recent
    while index > 0 and not is_boundary(messages, index):
        index -= 1
    return index if index > 0 else None


__all__ = ["cut_index", "is_boundary"]
