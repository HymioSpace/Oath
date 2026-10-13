"""`Intake` —— 系统里唯一的入口。

两条通路，都只跟 journal 打交道：

- **入站**：接收 command 落进 journal，返回游标给传输层去 ack。它到这一步就结束 ——
  一条记录落盘时不带任何调度结论。
- **消费**：从三条队列取记录，逐条问 `EventRouter` 走规则还是走 `Brain`，然后推进游标。

**它没有外部副作用。** append 按 command id 幂等，崩在中途不会留下半吊子的状态 ——
已 commit 的记录不再来，没 commit 的下次重来。所以游标由它推进：`Brain.step` 返回之后
才算处理完，返回之后再崩就是重复调模型。

它不解析帧。socket、握手、JSON 解码是传输层的事，传输解析出 `Command` 再交给这里；
core 不管自己怎么被连上来的。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Final

from core.protocol.datatype.cursor import Cursor
from core.protocol.datatype.journal import Record
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import Command


class RoutingDecision:
    """一条队列记录取出之后归谁处理。"""

    #: 交给 `Brain`：这是 agent 该回应的输入。
    TO_BRAIN: Final = "to_brain"
    #: core 按既定规则处理掉：`run.cancel` 中止运行、审批回执入账之类。
    BY_RULE: Final = "by_rule"

    ALL: Final = frozenset({TO_BRAIN, BY_RULE})


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    """credential 换来的身份。连接建立时定下来，之后这条连接不再自称。"""

    user_id: str
    session_id: str


class SessionAuthenticator(ABC):
    """把 credential 换成 `SessionIdentity`。

    v1 没有实现 —— 渠道接入方式定了才有。刻意不返回 None 表示「认不出来」：
    那是异常，不是可选项。不回落到匿名账号。
    """

    @abstractmethod
    def authenticate(self, credential: str) -> SessionIdentity:
        """认不出就抛错 —— 不回落到匿名账号。"""
        ...


class EventRouter(ABC):
    """这条队列记录交给 `Brain` 还是 core 自己按规则处理掉。

    判断的是记录，不是原始 command：一条 `run.cancel` 落盘时和被读出来时，中间隔着
    别的记录，理由可能已经变了。

    v1 没有实现。接口存在的理由是：每条记录的去向必须有唯一的决定点，不能散落在
    `poll` 里对 type 的 if 串中。
    """

    @abstractmethod
    def route(self, record: Record) -> str:
        """返回一个 `RoutingDecision`。"""
        ...


class Intake(ABC):
    """系统的入口与队列消费循环。"""

    @abstractmethod
    def open_session(self, command: Command, *, authenticator: SessionAuthenticator) -> Scope:
        """处理 `session.open`：验 credential、绑定 session，返回这个 session 的 scope。"""
        ...

    @abstractmethod
    def handle(self, command: Command, scope: Scope) -> Cursor | None:
        """落一条 command，返回它的游标，供传输层 ack。

        返回 None 表示这条 command 不落盘（`ping`）。成功即代表已持久化；抛错表示
        没落成，调用方不得 ack。
        """
        ...

    @abstractmethod
    def poll(self, scope: Scope) -> bool:
        """把这个 scope 三条队列里可读的记录取出来，逐条处理掉。

        每条记录问一次 `EventRouter`：`TO_BRAIN` 交给 `Brain.step`，`BY_RULE` 由 core
        自己处理。处理完一条 commit 一条。

        返回 True 表示至少处理了一条。**返回 False 不代表出错**：队列空就是队列空。
        处理途中崩了，下次会重来 —— `Intake` 没有副作用。
        """
        ...

    @abstractmethod
    def wait(self, scope: Scope, *, timeout: float | None = None) -> bool:
        """等到有可读记录再 `poll`。超时返回 False。

        空转轮询 journal 是浪费 IO，也是把「有没有事做」与「谁负责等」搞混。
        """
        ...

    @abstractmethod
    def close(self, *, wait: bool = True) -> None:
        """停机。`wait=False` 不等正在跑的 `poll` 返回。

        停机不写任何「收尾」记录：没处理的输入在 journal 里，下次起来还在。
        """
        ...


__all__ = [
    "EventRouter",
    "Intake",
    "RoutingDecision",
    "SessionAuthenticator",
    "SessionIdentity",
]
