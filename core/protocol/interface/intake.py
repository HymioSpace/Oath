"""`Intake` —— 系统里唯一的入口。

一条通路，顺序固定：**先落 journal，再入队，消费时才做决策。**

- **入站**：接收 command，append 进 journal，把落好的记录扔进它该在的那条队列。落盘
  在排队之前 —— 队列里不会有没写进日志的东西。
- **消费**：从队列取出记录，逐条问 `EventRouter` 走规则还是走 `Brain`。

**入队就是全部的缓冲。** 队列取出即出队，没有位置要推进。崩溃时队列清空，里面没处理
过的记录还留在 journal 里 —— 真相在 journal，不在队列；那些记录要靠上层决定重启后怎么
办。

它不解析帧。socket、握手、JSON 解码是传输层的事，传输解析出 `Command` 再交给这里；
core 不管自己怎么被连上来的。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Final

from core.protocol.datatype.journal import Record
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import Command


class RoutingDecision:
    """一条记录出队之后归谁处理。"""

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
    def handle(self, command: Command, scope: Scope) -> None:
        """落一条 command 并入队。

        返回即代表已持久化；抛错表示没落成，调用方不得回执成功。有些 command 不产生
        记录（`ping`），那不算错。
        """
        ...

    @abstractmethod
    def poll(self, scope: Scope) -> bool:
        """把这个 scope 三条队列里的记录取出来，逐条处理掉。

        每条记录问一次 `EventRouter`：`TO_BRAIN` 交给 `Brain.step`，`BY_RULE` 由 core
        自己处理。

        返回 True 表示至少处理了一条。**返回 False 不代表出错**：队列空就是队列空。
        崩了不会丢 —— 出队前记录已经在 journal 里。
        """
        ...

    @abstractmethod
    def wait(self, scope: Scope, *, timeout: float | None = None) -> bool:
        """等到有可读记录再 `poll`。超时返回 False。

        空转轮询是浪费 CPU，也是把「有没有事做」与「谁负责等」搞混。
        """
        ...

    @abstractmethod
    def close(self, *, wait: bool = True) -> None:
        """停机。`wait=False` 不等正在跑的 `poll` 返回。

        停机不写任何「收尾」记录：队列里剩下的还在 journal 里。
        """
        ...


__all__ = [
    "EventRouter",
    "Intake",
    "RoutingDecision",
    "SessionAuthenticator",
    "SessionIdentity",
]
