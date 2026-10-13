"""装配：把配置变成一个能跑的 runtime。

依赖方向单向 —— `bootstrap` → `core`。core 不反向 import 这里任何东西。

一个 session 一个消费线程：线程在 `session.open` 时起，`Brain.step` 就在这条线程上跑，
所以模型调用的阻塞只挡住那一条会话。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Final

from core.agent import AgentBrain
from core.context import ContextManager, NullCompressor
from core.intake import JournalIntake, ToolResultRouter, TypeRouter
from core.journal.impl import FileJournal
from core.llm import OpenAICompatibleProvider
from core.outbound import OutboundHub, WsEventSink
from core.protocol.datatype.config import OathConfig
from core.protocol.datatype.scope import Scope
from core.protocol.error import PermissionDeniedError
from core.protocol.interface.brain import Brain
from core.protocol.interface.event import AgentEventSink
from core.protocol.interface.intake import Intake, SessionAuthenticator, SessionIdentity
from core.protocol.interface.permission import PermissionPolicy
from core.protocol.interface.tool import Tool, ToolSystem
from core.queue import InMemoryEventQueue
from core.runtime import AgentContext
from core.tool.impl import ThreadedToolSystem
from core.tool.permission import PermissionManager
from core.tool.permission.impl import FilePermissionStore
from core.tool.registry import ToolRegistry
from core.utils.encoding import JsonObject

#: 空闲多久之后醒一次看一眼有没有新输入。等不是必须的，队列空就是空。
_WORKER_TICK: Final = 0.2


class TokenAuthenticator(SessionAuthenticator):
    """查表换身份。

    **v1 的演示实现**：配置里写死 credential → 身份的映射。生产由 Hymio 提供 ——
    认不出就抛错，不回落到匿名账号。
    """

    def __init__(self, credentials: dict[str, tuple[str, str]]) -> None:
        self._credentials = credentials

    def authenticate(self, credential: str) -> SessionIdentity:
        identity = self._credentials.get(credential)
        if identity is None:
            raise PermissionDeniedError(f"credential 不认: {credential!r}")
        user_id, session_id = identity
        return SessionIdentity(user_id=user_id, session_id=session_id)


class AllowAllPolicy(PermissionPolicy):
    """全部放行。

    **演示用的。** 每次工具执行前都要过一次判定是真的，但「判什么」是应用侧策略 ——
    Hymio 接进来时换成它自己的实现。
    """

    def check(self, scope: Scope, tool: str, arguments: JsonObject) -> str:
        return "allow"


@dataclass
class SessionWorker:
    """一个 session 的消费线程。"""

    scope: Scope
    thread: threading.Thread
    stop: threading.Event = field(default_factory=threading.Event)


class Runtime:
    """装配好的 runtime。"""

    def __init__(
        self,
        config: OathConfig,
        *,
        loop: asyncio.AbstractEventLoop,
        tools: list[Tool] | None = None,
    ) -> None:
        self.config = config
        self.authenticator = TokenAuthenticator(config.credentials)

        data_root = config.data_root
        # 关停要调 `close`，而 `Journal` 接口上没有这个方法 —— 那是具体实现的
        # 生命周期，装配层握着自己造出来的那个对象。
        self.journal = FileJournal(data_root / "journal")
        queue = InMemoryEventQueue()
        self.hub = OutboundHub(loop, send_queue_size=config.server.send_queue_size)
        outbound: AgentEventSink = WsEventSink(self.hub)
        # 工具系统的终态事件从工作线程来，没人替它落盘 —— 这一层补上，
        # 顺带把它放回 tool 队列，下一次 step 才接得上。brain 发的事件原样穿过。
        sink: AgentEventSink = ToolResultRouter(self.journal, queue, outbound)

        registry = ToolRegistry(default_timeout_seconds=config.agent.tool_timeout_seconds)
        for tool in tools or []:
            registry.register(tool)
        tool_system: ToolSystem = ThreadedToolSystem(
            registry,
            sink,
            PermissionManager(
                FilePermissionStore(data_root / "permissions"),
                AllowAllPolicy(),
                sink,
            ),
        )

        # 共享基础设施收口成一份。各组件按自己的需要从这里取，不用各自接一遍。
        self.ctx = AgentContext(
            journal=self.journal,
            queue=queue,
            sink=sink,
            tools=tool_system,
            context_manager=ContextManager(self.journal, config.agent, compressor=NullCompressor()),
            config=config.agent,
        )
        self.brain: Brain = AgentBrain(self.ctx, OpenAICompatibleProvider(config.agent.provider))
        self.intake: Intake = JournalIntake(self.ctx, self.brain, TypeRouter())
        self._workers: dict[Scope, SessionWorker] = {}
        self._workers_lock = threading.Lock()

    # -- session 生命周期 ---------------------------------------------------

    def ensure_session(self, scope: Scope) -> None:
        """给这个 scope 起一条消费线程。已经有了就什么都不做。"""
        with self._workers_lock:
            if scope in self._workers:
                return
            stop = threading.Event()
            thread = threading.Thread(
                target=self._consume, args=(scope, stop), name=f"oath-session-{scope}", daemon=True
            )
            self._workers[scope] = SessionWorker(scope=scope, thread=thread, stop=stop)
            thread.start()

    def _consume(self, scope: Scope, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                if self.intake.wait(scope, timeout=_WORKER_TICK):
                    self.intake.poll(scope)
            except Exception:
                # 一条输入处理失败不能让线程死掉 —— 剩下的输入还在队列里。
                # 失败已经由 `AgentBrain` 记成 `system.agent_step{error}`。
                continue

    def close(self) -> None:
        with self._workers_lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.stop.set()
        for worker in workers:
            worker.thread.join(timeout=2.0)
        self.intake.close(wait=True)
        self.ctx.tools.shutdown(wait=True)
        self.hub.shutdown()
        self.journal.close()


__all__ = ["AllowAllPolicy", "Runtime", "SessionWorker", "TokenAuthenticator"]
