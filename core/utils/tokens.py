"""占用估算：ASCII 每 4 字符记 1 token，其余记 1 token。

**这是估算，不是计数。** 真正的 token 数只有 provider 知道，而阈值本来就是启发式的。
要精确就换掉这个函数 —— 阈值判断只依赖它返回的相对大小。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

#: 低于这个码位的字符按「便宜」算。之上基本是全角文字和标点，一字符一个 token。
_CHEAP_BELOW = 0x2E80
_CHARS_PER_TOKEN = 4


def _cost(text: str) -> float:
    return sum(1.0 if ord(char) >= _CHEAP_BELOW else 1 / _CHARS_PER_TOKEN for char in text)


def estimate_text(text: str) -> int:
    return math.ceil(_cost(text))


def estimate(messages: Sequence[Mapping[str, Any]]) -> int:
    """一段消息的 token 估算。`tool_calls` 的参数一并计入 —— 它同样进请求体。"""
    total = 0.0
    for message in messages:
        total += _cost(str(message.get("content", "")))
        for call in message.get("tool_calls") or ():
            total += _cost(str(call.get("name", "")))
            total += _cost(str(call.get("arguments", "")))
    return math.ceil(total)


__all__ = ["estimate", "estimate_text"]
