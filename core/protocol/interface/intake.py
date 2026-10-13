"""`Intake` —— 系统里唯一的入口。

它做两件事，都只跟 journal 打交道：

1. **入站**：接收 command，落进 journal。
2. **消费**：从三条队列取记录，分发给 `Brain`，推进游标。

这两件事放在一起不是因为「方便」，而是因为它们**失败语义相同**：append 按 command
id 幂等，没有外部副作用，崩了下次重来就行。分开成两个组件，中间那道缝是假的 —— 一个
写完记录什么也不做，另一个再去把它读出来。游标归它也是同一个理由：记账规则与
`Brain` 无关。

**`Intake` 不做任何决策。** 「这条要不要给 agent」是 `EventRouter` 的事，「跑什么」是
`Brain` 的事。它只负责让事件可读且有序。

它也不会解析帧：socket、握手、JSON 解码是传输层的事，传输解析出 `Command` 再交给这里。
core 不管自己怎么被连上来的。

`Brain` 可能已经调过模型、派发过工具，它崩了之后哪些能重放是策略 —— 所以 `Brain` 与
`Intake` 分开，而入站与消费不分。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Final

from core.protocol.datatype.cursor import Cursor
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import Command


class RoutingDecision:
    """一条入站记录落盘之后的结论。"""

    #: 落盘了，该让 `Brain` 看一眼。
    DISPATCH: Final = "dispatch"
    #: 落盘了（或什么都没做），`Brain` 不必动。`ping`、`session.open` 走这里。
    ACK_ONLY: Final = "ack_only"

    ALL: Final = frozenset({DISPATCH, ACK_ONLY})


@dataclass(frozen=True, slots=True)
class RoutingOutcome:
    """`handle` 的返回。`scope` 是记录落到的位置。"""

    scope: Scope
    decision: str
    #: 落盘后那条记录的位置。`ACK_ONLY` 且没落盘时是 None。
    cursor: Cursor | None


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
    """这条入站记录要不要交给 `Brain`。

    v1 没有实现。接口存在的理由是：`Intake` 要被唤醒这件事必须有唯一的决定点，
    不能散落在入站路径与消费路径各自的 if 里。
    """

    @abstractmethod
    def route(self, command: Command) -> str:
        """返回一个 `RoutingDecision`。"""
        ...


class Intake(ABC):
    """系统的入口与队列消费循环。"""

    @abstractmethod
    def open_session(
        self, command: Command, *, authenticator: SessionAuthenticator
    ) -> RoutingOutcome:
        """处理 `session.open`：验 credential、绑定 session。"""
        ...

    @abstractmethod
    def handle(self, command: Command, scope: Scope) -> RoutingOutcome:
        """落一条 command。

        返回成功即代表记录已持久化，游标已推进。抛错表示没落成，调用方不得 ACK。
        """
        ...

    @abstractmethod
    def poll(self, scope: Scope) -> bool:
        """把这个 scope 三条队列里可读的记录取出来、分发给 `Brain`。

        返回 True 表示至少分发了一条。**返回 False 不代表出错**：队列空就是队列空。
        分发途中崩了，下次会重来 —— `Intake` 没有副作用。
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
    "RoutingOutcome",
    "SessionAuthenticator",
    "SessionIdentity",
]
