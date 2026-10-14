"""`NullCompressor` —— 不压缩的那一个。

v1 没有压缩。留一个恒等的实现，好过让 `ContextManager` 里出现一个 `if compressor is
None` 的分支：分支会让人以为压缩已经在那儿了。
"""

from __future__ import annotations

from core.protocol.datatype.agent_state import AgentState
from core.protocol.datatype.message import Message
from core.protocol.interface.context import ContextCompressor


class NullCompressor(ContextCompressor):
    """永远说不压，压了也原样返回。"""

    def should_compress(self, messages: list[Message], state: AgentState) -> bool:
        return False

    def compress(
        self, messages: list[Message], state: AgentState
    ) -> tuple[list[Message], AgentState]:
        return messages, state


__all__ = ["NullCompressor"]
