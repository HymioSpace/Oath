"""`Compactor` —— 把一段对话历史换成一个短的等价物。

压缩只做这一件事：给定一段消息，返回替代它的消息。切在哪、什么时候切、切完要不要生效
都不归它管 —— 那是 `core/compaction` 的策略。

返回 `None` 表示压不了。调用方据此保留原历史：压缩失败降级成「不压」，不是错误。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any


class CompactionError(RuntimeError):
    """A compaction attempt that produced no usable artifact."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Compactor(ABC):
    """历史压缩算法。"""

    @abstractmethod
    def compress(
        self, messages: Sequence[Mapping[str, Any]], *, budget_tokens: int
    ) -> list[dict[str, Any]] | None:
        """压缩一段历史。

        `budget_tokens` 是这段压缩结果的规模上限，算法自己决定怎么用。
        返回的消息必须是 provider 能接受的形态，且首条不能是 `tool` role。

        实现必须不修改入参，也不产生副作用。
        """
        ...


__all__ = ["CompactionError", "Compactor"]
