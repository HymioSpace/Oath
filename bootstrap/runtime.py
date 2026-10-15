"""装配三 consumer、独立 Brain 和共用持久化事务。"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Final

from core.agent import AgentBrain
from core.buffer.journal_buffer import JournalEventBuffer
from core.compaction import CompactionCoordinator, LlmCompactor
from core.consumer import SystemConsumer, ToolConsumer, UserConsumer
from core.context import ContextManager
from core.detail import DETAIL_READ, DetailReadTool, ResultShrinker
from core.detail.impl import FileDetailStore
from core.journal.impl import FileJournal
from core.journal.runtime_store import RuntimeStore
from core.llm import OpenAICompatibleProvider
from core.outbound import OutboundHub, WsEventSink
from core.protocol.datatype.config import OathConfig
from core.protocol.datatype.inbound import of_command
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import Command, CommandType
from core.protocol.error import PermissionDeniedError, WireProtocolError
from core.protocol.interface.brain import Brain
from core.protocol.interface.event import AgentEventSink
from core.protocol.interface.llm import ModelProvider
from core.protocol.interface.permission import PermissionPolicy
from core.protocol.interface.session import SessionAuthenticator, SessionIdentity
from core.protocol.interface.tool import Tool
from core.queue import InMemoryEventQueue
from core.runtime import AgentContext
from core.skill import SKILL, SkillSystem, SkillTool
from core.tool.impl import ThreadedToolSystem
from core.tool.registry import ToolRegistry
from core.trace.impl import FileTraceStore
from core.utils.encoding import JsonObject

#: 停机时等消费线程退出的上限。它阻塞在 `queue.wait` 上，`close` 会把它叫醒。
_WORKER_JOIN_TIMEOUT: Final = 2.0

_log = logging.getLogger("oath.runtime")


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
        policy: PermissionPolicy | None = None,
        provider: ModelProvider | None = None,
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
        store = RuntimeStore(self.journal)
        details = FileDetailStore(data_root / "details")
        self.traces = FileTraceStore(data_root / "traces")
        skills = SkillSystem(config.agent.skill_dir, scan_dirs=config.agent.skill_scan_dirs)
        registry = ToolRegistry(default_timeout_seconds=config.agent.tool_timeout_seconds)
        registry.register(DetailReadTool(details))
        registry.register(SkillTool(skills))
        for tool in tools or []:
            registry.register(tool)
        tool_system = ThreadedToolSystem(
            registry,
            outbound,
            policy or AllowAllPolicy(),
            store=store,
            queue=queue,
            shrinker=ResultShrinker(
                details,
                config.agent.tool_result_detail_chars,
                whole=frozenset({DETAIL_READ, SKILL}),
            ),
        )
        self.ctx = AgentContext(
            journal=self.journal,
            store=store,
            queue=queue,
            sink=outbound,
            tools=tool_system,
            context_manager=ContextManager(self.journal, config.agent),
            skills=skills,
            traces=self.traces,
            config=config.agent,
        )
        model = provider or OpenAICompatibleProvider(config.agent.provider)
        self.compaction = CompactionCoordinator(
            store,
            LlmCompactor(
                model,
                model=config.agent.provider.model,
                max_output_tokens=config.agent.max_output_tokens,
            ),
            config.agent,
            sink=outbound,
        )
        self.brain: Brain = AgentBrain(self.ctx, model, self.compaction)
        self.buffers = JournalEventBuffer(
            store, tool_buffer_timeout=config.agent.tool_round_timeout_seconds
        )
        self.consumers = (
            UserConsumer(self.ctx, self.brain, self.buffers),
            ToolConsumer(self.ctx, self.brain, self.buffers),
            SystemConsumer(self.ctx, self.brain, self.buffers),
        )
        self._workers: dict[tuple[Scope, str], SessionWorker] = {}
        self._workers_lock = threading.Lock()
        self._resume_buffered()

    # -- 上行 --------------------------------------------------------------

    def open_session(self, command: Command) -> Scope:
        """验 credential，绑定 session。认不出就抛错 —— 不回落到匿名账号。"""
        identity = self.authenticator.authenticate(str(command.payload.get("credential", "")))
        return Scope(user_id=identity.user_id, session_id=identity.session_id)

    def submit(self, command: Command, scope: Scope) -> None:
        if command.session_id != scope.session_id:
            raise WireProtocolError("session 与认证身份不匹配", code="session_mismatch")
        if command.type == CommandType.PERMISSION_DECIDE:
            if command.payload.get("status") not in {"approved", "denied"}:
                raise WireProtocolError("审批只接受 approved / denied", code="bad_payload")
            if not isinstance(command.payload.get("invocation_id"), str):
                raise WireProtocolError("审批缺 invocation_id", code="bad_payload")
            if self.ctx.tools.status(scope, str(command.payload["invocation_id"])) is None:
                raise WireProtocolError("找不到调用", code="not_found")
            command = Command(
                command.id,
                command.type,
                command.session_id,
                {**command.payload, "decided_by": scope.user_id},
            )
        if command.type == CommandType.USER_MESSAGE and not isinstance(
            command.payload.get("content"), str
        ):
            raise WireProtocolError("content 必须是文本", code="bad_payload")
        inbound = of_command(command)
        if inbound is not None:
            self.buffers.accept(scope, inbound)
            self.ctx.queue.put(scope, inbound)
            self.ensure_session(scope)

    def status(self, scope: Scope) -> JsonObject:
        state = self.ctx.store.read(scope)
        active = state["meta"].get("active_step")
        step = state["steps"].get(active, {})
        return {
            "run_id": state["meta"].get("run_id"),
            "step_id": active,
            # 客户端拿 trace_id 去查这条运行：认领 step 时定的，之后不变。
            "trace_id": step.get("trace_id"),
            "step_status": step.get("status", "idle"),
            "error": step.get("error") or state["meta"].get("intake_error"),
            "calls": list(state["calls"].values()),
            "messages": state["conversation"].get("messages", []),
            "compaction": {
                key: {
                    field: value.get(field) for field in ("compression_id", "covers", "created_dt")
                }
                for key, value in state["compactions"].items()
            },
            "input_errors": [
                {"input_id": key, "error": entry["error"]}
                for key, entry in state["inputs"].items()
                if entry["phase"] == "rejected"
            ],
        }

    def ensure_session(self, scope: Scope) -> None:
        with self._workers_lock:
            for consumer in self.consumers:
                key = (scope, consumer.queue_name)
                existing = self._workers.get(key)
                if existing and existing.thread.is_alive():
                    continue
                stop = threading.Event()
                thread = threading.Thread(
                    target=consumer.run,
                    args=(scope, stop),
                    name=f"oath-{consumer.queue_name}-{scope.session_id}",
                    daemon=True,
                )
                self._workers[key] = SessionWorker(scope, thread, stop)
                thread.start()
        self.brain.resume(scope)

    def _resume_buffered(self) -> None:
        for scope in self.ctx.store.scopes():
            self.ctx.tools.recover_scope(scope)
            self.ensure_session(scope)

    def close(self) -> None:
        with self._workers_lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.stop.set()
        # 先断队列：消费线程正阻塞在 `wait` 上，不叫醒它就只能靠 join 超时。
        self.ctx.queue.close(wait=True)
        for worker in workers:
            worker.thread.join(timeout=_WORKER_JOIN_TIMEOUT)
            if worker.thread.is_alive():
                # consumer 只做短事务；超时需排查存储或控制处理。
                _log.warning(
                    "session %s 的消费线程没能在 %ss 内退出", worker.scope, _WORKER_JOIN_TIMEOUT
                )
        self.brain.close(wait=True)
        self.compaction.shutdown()
        self.ctx.tools.shutdown(wait=True)
        self.hub.shutdown()
        self.journal.close()


__all__ = ["AllowAllPolicy", "Runtime", "SessionWorker", "TokenAuthenticator"]
