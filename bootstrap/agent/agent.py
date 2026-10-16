"""一个 scope 的执行状态所有者。

进程内固定三个 consumer 线程（user / tool / system）和一个 Brain 线程。consumer 从
进程外的队列收输入、放进持久 buffer、决定唤醒；Brain 只看 buffer 有没有东西，有就
认领一次落进 journal，然后走 context build → 模型 → 工具。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

from redis import Redis

from bootstrap.agent.service import SessionService
from bootstrap.common.authentication import (
    SESSION_CREDENTIAL_ENV,
    TokenAuthenticator,
    read_session_credential,
)
from bootstrap.common.extension_loader import load_extensions
from core.agent import AgentBrain
from core.buffer import DiskEventBuffer
from core.compaction import CompactionCoordinator, LlmCompactor
from core.consumer import SystemConsumer, ToolConsumer, UserConsumer
from core.context import ContextManager
from core.context.user_context import current, table_of
from core.detail import DETAIL_READ, DetailReadTool, ResultShrinker
from core.detail.impl import FileDetailStore
from core.journal.impl import FileJournal
from core.journal.runtime_store import RuntimeStore
from core.llm import OpenAICompatibleProvider
from core.outbound import ClientChannel, OutboundHub, WsEventSink
from core.protocol.datatype.config import OathConfig
from core.protocol.datatype.inbound import of_command
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import Command, CommandType
from core.protocol.error import WireProtocolError
from core.protocol.interface.brain import Brain
from core.protocol.interface.event import AgentEventSink
from core.protocol.interface.llm import ModelProvider
from core.protocol.interface.permission import PermissionPolicy
from core.protocol.interface.tool import Tool
from core.protocol.interface.trace import TraceStore
from core.queue.redis_queue import RedisEventQueue
from core.runtime import AgentContext
from core.shell import BashTool, HostEnvironment
from core.skill import SKILL, SkillSystem, SkillTool
from core.tool.impl import ThreadedToolSystem
from core.tool.registry import ToolRegistry
from core.trace.impl import FileTraceStore
from core.utils.encoding import JsonObject, JsonValue

#: 停机时等消费线程退出的上限。它阻塞在 `listen` 上，`close` 会把它叫醒。
_WORKER_JOIN_TIMEOUT: Final = 2.0

_log = logging.getLogger("oath.agent")


class AllowAllPolicy(PermissionPolicy):
    """全部放行。

    **演示用的。** 每次工具执行前都要过一次判定是真的，但「判什么」是应用侧策略 ——
    Hymio 接进来时换成它自己的实现。
    """

    def check(self, scope: Scope, tool: str, arguments: JsonObject) -> str:
        return "allow"


def _check_user_context(value: JsonValue) -> None:
    """`user_context` 必须是 key 非空的 object。

    值不额外限制：这一层不解释内容，它只保证「这是个 kv」，值是什么由业务命令负责。空 key
    是拼错字段名的典型症状，放进来会让后面覆盖时看不出是哪一条。
    """
    if not isinstance(value, dict):
        raise WireProtocolError("user_context 必须是对象", code="bad_payload")
    if any(not isinstance(key, str) or not key for key in value):
        raise WireProtocolError("user_context 的 key 必须是非空字符串", code="bad_payload")


def _cli_env(config: OathConfig) -> Callable[[Scope], Mapping[str, str]]:
    """按 scope 现算 CLI 客户端要用的环境变量。

    **应用侧怎么鉴权就怎么给。** 这里只把 biz 的地址和本 scope 的 session token 递出去；
    换成别的认证方式，改的是这一处。
    """
    host = config.server.host
    server = f"http://{host}:{config.server.http_port}"
    token = read_session_credential()

    def env_for(scope: Scope) -> Mapping[str, str]:
        return {"OATH_CLI_SERVER": server, "OATH_CLI_TOKEN": token}

    return env_for


@dataclass
class SessionWorker:
    """一条队列的消费线程。"""

    scope: Scope
    thread: threading.Thread
    stop: threading.Event = field(default_factory=threading.Event)


class Agent(SessionService):
    """**一个进程一个 scope。** 进程外只有队列与 buffer，进程内没有任何别的 scope。"""

    def __init__(
        self,
        config: OathConfig,
        scope: Scope,
        *,
        loop: asyncio.AbstractEventLoop,
        credential: str,
        redis: Redis,
        tools: list[Tool] | None = None,
        policy: PermissionPolicy | None = None,
        provider: ModelProvider | None = None,
    ) -> None:
        """`tools` 是装配应用显式注入的工具 —— **不是**第二条发现机制：注入进来的那个
        实例可以带着配置好的依赖，扩展目录里的工具只能零参构造。扫出来的排在注入的前面，
        撞名由注册表报出来，不用猜是谁先注册的。
        """
        self.config, self.scope = config, scope
        self._loop = loop
        self.authenticator = TokenAuthenticator({credential: (scope.user_id, scope.session_id)})
        # 扩展在装配期就装好：装不起来要在这里炸，不用等到模型真的去调它。
        extensions = load_extensions(
            config.agent.extension_dirs, kinds=frozenset({"tools", "skills"})
        )

        # 落盘的三个 store 自己按 scope 分目录，这里给的都是根。
        self.journal = FileJournal(config.data_root / "journal")
        queue = RedisEventQueue(redis)
        self.buffer = DiskEventBuffer(config.data_root / "buffer")
        self.hub = OutboundHub(loop, send_queue_size=config.server.send_queue_size)
        outbound: AgentEventSink = WsEventSink(self.hub)
        store = RuntimeStore(self.journal)
        details = FileDetailStore(config.data_root / "details")
        self.traces: TraceStore = FileTraceStore(config.data_root / "traces")
        skills = SkillSystem(
            (config.agent.skill_dir, *extensions.skill_dirs),
            scan_dirs=config.agent.skill_scan_dirs,
        )
        registry = ToolRegistry(default_timeout_seconds=config.agent.tool_timeout_seconds)
        registry.register(DetailReadTool(details))
        registry.register(SkillTool(skills))
        # 执行环境选的是宿主机实现。沙箱要换掉的是这一个类 —— bash 工具与 CLI server
        # 都只认接口。
        shell = HostEnvironment(config.agent.workspace_root, bin_dirs=(config.agent.cli_bin_dir,))
        registry.register(BashTool(shell, _cli_env(config), root=config.agent.workspace_root))
        for tool in extensions.tools:
            registry.register(tool)
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
            buffer=self.buffer,
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
        self.consumers = (
            UserConsumer(self.ctx, self.brain),
            ToolConsumer(self.ctx, self.brain),
            SystemConsumer(self.ctx, self.brain),
        )
        self._workers: list[SessionWorker] = []
        self._started = False

    # -- 生命周期 ----------------------------------------------------------

    def start(self) -> None:
        """起三个消费线程，并把在飞的工具收尾。

        **重启时先恢复再收输入。** 上一个进程可能崩在工具执行中途 —— 那些调用一律报
        `outcome_unknown`，不能重跑。
        """
        if self._started:
            return
        self._started = True
        self.ctx.tools.recover_scope(self.scope)
        for consumer in self.consumers:
            stop = threading.Event()
            thread = threading.Thread(
                target=consumer.run,
                args=(self.scope, stop),
                name=f"oath-{consumer.queue_name}-{self.scope.session_id}",
                daemon=True,
            )
            self._workers.append(SessionWorker(self.scope, thread, stop))
            thread.start()
        self.brain.resume(self.scope)

    @property
    def connections(self) -> int:
        """在线连接数。manager 按它判断这个 scope 还有没有人用。"""
        return self.hub.connections

    # -- 上行 --------------------------------------------------------------

    def open_session(self, command: Command) -> Scope:
        """验 credential。认不出就抛错 —— 不回落到匿名账号。"""
        identity = self.authenticator.authenticate(str(command.payload.get("credential", "")))
        if (identity.user_id, identity.session_id) != (self.scope.user_id, self.scope.session_id):
            raise WireProtocolError("credential 不属于这个 session", code="session_mismatch")
        return self.scope

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
        if command.type == CommandType.USER_MESSAGE:
            if not isinstance(command.payload.get("content"), str):
                raise WireProtocolError("content 必须是文本", code="bad_payload")
            _check_user_context(command.payload.get("user_context"))
        inbound = of_command(command)
        # 只投队列。落盘是 Brain 认领时的事，这里写 journal 就把两个答案写在一起了。
        if inbound is not None:
            self.ctx.queue.add(scope, inbound)

    def status(self, scope: Scope) -> JsonObject:
        state = self.ctx.store.read(scope)
        active = state["meta"].get("active_step")
        step = state["steps"].get(active, {})
        return {
            "run_id": state["meta"].get("run_id"),
            "step_id": active,
            # 客户端拿 trace_id 去查这条运行：认领 step 时定的，之后不变。没有在飞的
            # step 时退回最后开过的那条 —— 否则刚结束的一轮就查不到了。
            "trace_id": step.get("trace_id") or state["meta"].get("trace_id"),
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
        }

    def user_context(self, scope: Scope) -> JsonObject:
        return current(table_of(self.ctx.store.read(scope)), now=time.time())

    # -- 下行投递 ----------------------------------------------------------

    def attach(self, scope: Scope, channel: ClientChannel) -> None:
        self.hub.attach(scope, channel)

    def detach(self, scope: Scope, channel: ClientChannel) -> None:
        self.hub.detach(scope, channel)

    def close(self) -> None:
        for worker in self._workers:
            worker.stop.set()
        # 先断队列：消费线程正阻塞在 `listen` 上，不叫醒它就只能靠 join 超时。
        self.ctx.queue.close(wait=True)
        for worker in self._workers:
            worker.thread.join(timeout=_WORKER_JOIN_TIMEOUT)
            if worker.thread.is_alive():
                # consumer 只做短事务；超时需排查存储或控制处理。
                _log.warning(
                    "session %s 的消费线程没能在 %ss 内退出", worker.scope, _WORKER_JOIN_TIMEOUT
                )
        self.brain.close(wait=True)
        self.compaction.shutdown()
        self.ctx.tools.shutdown(wait=True)
        self.buffer.close()
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self.hub.shutdown)
        else:
            self.hub.shutdown()
        self.journal.close()


__all__ = ["SESSION_CREDENTIAL_ENV", "AllowAllPolicy", "Agent"]
