"""`ContextCompressor` —— 上下文压缩的接口。

**v1 没有实现。** 接口留着是因为压缩要同时改两样东西：messages 与 `AgentState`。
塞进 `ContextManager` 里会让那一个类两个职责，而且压缩错了很难查 —— 状态与对话
对不上时，模型会在一个它从没见过的状态上继续跑。

压缩是纯函数：给它一份 `(messages, state)`，它返回一份新的。原来的不动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.protocol.datatype.agent_state import AgentState
from core.protocol.datatype.message import Message


class ContextCompressor(ABC):
    """把过长的对话历史换成一个短的等价物。"""

    @abstractmethod
    def should_compress(self, messages: list[Message], state: AgentState) -> bool:
        """该不该压。返回 False 就原样用。"""

    @abstractmethod
    def compress(
        self, messages: list[Message], state: AgentState
    ) -> tuple[list[Message], AgentState]:
        """压。

        返回的 `state` 里要带上压缩后对话的说明（摘要本身、压掉的起止位置），
        否则下一次恢复会把它当没压过，又压一遍。
        """
        ...


__all__ = ["ContextCompressor"]
