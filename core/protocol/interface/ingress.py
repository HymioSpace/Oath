"""`Ingress` —— 入站事件的入口。

职责只有一件：**让入站事件变成 journal 里可读、有序的记录，并决定要不要唤醒
runtime**。它不执行任何东西 —— 那是 `Runtime` 的事。

两者的分工是持久化 vs 执行。放在 Ingress 的是「这条要不要让 agent 看见」这个
决定，接口层面叫 `EventRouter`。v1 没有实现：判断该不该给 agent 是策略，会随产品
形态变，写死在这里等于把第一次猜测当契约。

`user_id` **由服务端推导**。`session.open` 带的是 credential 不是 user_id ——
隔离键一旦能被调用方自称，越权读就不是漏洞而是默认行为（同一理由见 journal 的
`ScopeError`）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Final

from core.protocol.datatype.cursor import Cursor
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import Command


class RoutingDecision:
    """Ingress 处理完一条 command 之后的结论。"""

    #: 落盘了，runtime 该醒来看一眼。
    DISPATCH: Final = "dispatch"
    #: 落盘了（或什么都没做），runtime 不必动。`ping`、`session.open` 走这里。
    ACK_ONLY: Final = "ack_only"

    ALL: Final = frozenset({DISPATCH, ACK_ONLY})


@dataclass(frozen=True, slots=True)
class RoutingOutcome:
    """`handle` 的返回。`scope` 是记录落到的位置。"""

    scope: Scope
    decision: str
    #: 落盘后这条记录的位置。`ACK_ONLY` 且没落盘时是 None。
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
        """认不出就抛 `AuthenticationError`。"""
        ...


class EventRouter(ABC):
    """这条入站记录要不要交给 runtime。

    v1 没有实现。接口存在的理由是：runtime 要被唤醒这件事必须有唯一的决定点，
    不能散落在 Ingress 与 Runtime 各自的 if 里。
    """

    @abstractmethod
    def route(self, command: Command) -> str:
        """返回一个 `RoutingDecision`。"""
        ...


class Ingress(ABC):
    """入站 command 的处理器。"""

    @abstractmethod
    def open_session(
        self, command: Command, *, authenticator: SessionAuthenticator
    ) -> RoutingOutcome:
        """处理 `session.open`：验 credential、绑定 session、决定从哪个游标续推。"""
        ...

    @abstractmethod
    def handle(self, command: Command, scope: Scope) -> RoutingOutcome:
        """处理一条已绑定的 command：落 journal，然后按路由结论唤醒 runtime。

        返回成功即代表记录已持久化。抛错表示没落成，调用方不得 ACK。
        """
        ...

    @abstractmethod
    def close(self) -> None:
        """停掉内部资源。已经落盘的记录不受影响。"""
        ...


__all__ = [
    "EventRouter",
    "Ingress",
    "RoutingDecision",
    "RoutingOutcome",
    "SessionAuthenticator",
    "SessionIdentity",
]
