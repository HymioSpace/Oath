"""`ContextManager` —— 把 journal 变成一次模型请求要的东西。

一次 build 是三步纯读，没有写入：恢复、投影、拼 system prompt。**不写状态快照** ——
快照是加速恢复的，不是正确性来源；这一版 context 完全由 journal 投影出来，
重启之后照样能接着跑。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.journal.messages import project
from core.protocol.datatype.agent_state import AgentState
from core.protocol.datatype.config import AgentConfig
from core.protocol.datatype.cursor import Cursor, CursorKind
from core.protocol.datatype.message import Message
from core.protocol.datatype.tool import ToolDefinition
from core.protocol.datatype.user import User
from core.protocol.interface.journal import Journal

_BEGINNING = Cursor(CursorKind.ID, 0)


@dataclass(frozen=True, slots=True)
class Context:
    """一次模型请求要的全部东西，全是 provider 无关的。

    `state` 是这份 context 对应的状态。压缩会改它，所以它跟着 context 一起走，
    不是 build 之后随手丢掉的东西。
    """

    messages: list[Message]
    tools: tuple[ToolDefinition, ...]
    state: AgentState


class ContextManager:
    """从 journal 拼 context。"""

    def __init__(self, journal: Journal, config: AgentConfig) -> None:
        self._journal = journal
        self._config = config

    def build(self, user: User, tools: tuple[ToolDefinition, ...]) -> Context:
        """拼出截至此刻的完整对话。

        **一次投影。** `Recovery.messages` 已经是快照水位线之前那一段的投影了，再把
        `pending` 单独投影一遍拼起来看着省事，实际是把**一次调用和它的结果**劈成两半
        的做法 —— 快照提交在执行中途时，两半正好落在边界上，前一半得到一个
        `unresolved` 占位，后一半的结果被投影丢掉。日志里明明写着结果，模型却看不见。

        代价是整份日志读两遍（`recover` 一遍，这里一遍）。省下的那遍要用正确性换，
        不值。
        """
        recovery = self._journal.recover(user)
        messages = project(list(self._journal.read_range(user, _BEGINNING)))
        state = recovery.state

        return Context(
            messages=self._with_system_prompt(messages),
            tools=tools,
            state=state,
        )

    def _with_system_prompt(self, messages: list[Message]) -> list[Message]:
        """system prompt 放第一条。没有就不加。

        投影出来的消息里不会出现 system —— `project` 只产 user / assistant / tool。
        以后有了再说替换的逻辑，现在写它就是给不存在的情况写分支。
        """
        if not self._config.system_prompt:
            return list(messages)
        return [Message(role="system", content=self._config.system_prompt), *messages]


__all__ = ["Context", "ContextManager"]
