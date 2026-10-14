"""切点：切在哪里才不把一次工具调用和它的结果劈成两半。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def is_boundary(messages: Sequence[Mapping[str, Any]], index: int) -> bool:
    """`index` 能不能当切点。

    不能是 `tool` role（它是上面那次声明的结果），也不能带 `tool_calls`
    （它的结果还没出现）。两者都留着其余也留着，所以不会出现半个调用。
    """
    if not 0 <= index <= len(messages):
        return False
    if index == len(messages):
        return True
    message = messages[index]
    return message.get("role") != "tool" and not message.get("tool_calls")


def cut_index(messages: Sequence[Mapping[str, Any]], keep_recent: int) -> int | None:
    """保留末尾 `keep_recent` 条，返回切点。没有可压的历史返回 `None`。

    从 `len - keep_recent` 出发**向前**找合法切点：切得靠后一点，多留几条，
    而不是把保留条数减到刚好卡住。
    """
    if keep_recent < 1:
        raise ValueError(f"保留条数必须是正整数: {keep_recent!r}")
    index = len(messages) - keep_recent
    if index <= 0:
        return None
    while index < len(messages) and not is_boundary(messages, index):
        index += 1
    return index if index < len(messages) else None


__all__ = ["cut_index", "is_boundary"]
