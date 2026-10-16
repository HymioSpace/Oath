"""真实 Journal / 工具系统 / 三 consumer，模型与外部动作使用受控替身。"""

from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

from core.agent import AgentBrain
from core.buffer import DiskEventBuffer
from core.compaction import CompactionCoordinator
from core.consumer import SystemConsumer, ToolConsumer, UserConsumer
from core.context import ContextManager
from core.detail import DETAIL_READ, DetailReadTool, ResultShrinker
from core.detail.impl import FileDetailStore
from core.journal.impl import FileJournal
from core.journal.runtime_store import RuntimeStore, State
from core.protocol.datatype import Scope, ToolCallRequest
from core.protocol.datatype.config import AgentConfig
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.llm import LlmRequest, LlmResponse
from core.protocol.datatype.queue import QueueName
from core.protocol.interface.compaction import Compactor
from core.protocol.interface.llm import DeltaSink, ModelProvider
from core.protocol.interface.permission import PermissionPolicy
from core.protocol.interface.queue import EventQueue
from core.protocol.interface.tool import Tool
from core.protocol.interface.trace import TraceStore
from core.queue import InMemoryEventQueue
from core.runtime import AgentContext
from core.skill import SKILL, SkillSystem, SkillTool
from core.tool.impl import ThreadedToolSystem
from core.tool.registry import ToolRegistry
from tests._support.tool_fixtures import RecordingSink, VerdictPolicy
from tests._support.trace_fixtures import RecordingTraceStore

SCOPE = Scope("u1", "s1")

#: 拿到请求之后当场生成的回答 —— 用它控制「模型什么时候说话」。
ResponseScript = Callable[[LlmRequest], LlmResponse]
#: 脚本接受三种 response：写死的回答、当场生成的回答、要抛出的异常。
ScriptedResponse = LlmResponse | ResponseScript | BaseException

_T = TypeVar("_T")


def until(predicate: Callable[[], _T], timeout: float = 3) -> _T:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(0.005)
    raise AssertionError("等待状态变化超时")


def arrived(harness: Harness, queue: str) -> bool:
    """队列里已经没有东西了 —— 那条输入已经被 consumer 取走，放进 buffer 了。

    **不碰 buffer。** `get_all` 是取走，验「东西到了没有」不能拿它当观察手段；
    队列空不空才是这里要的信号。
    """
    return not harness.queue.listen(harness.scope, queue, 0.05)


class ScriptedProvider(ModelProvider):
    def __init__(self, responses: Sequence[ScriptedResponse] = ()) -> None:
        self.responses: list[ScriptedResponse] = list(responses)
        self.requests: list[LlmRequest] = []
        self.condition = threading.Condition()

    def complete(self, request: LlmRequest, *, on_delta: DeltaSink | None = None) -> LlmResponse:
        with self.condition:
            index = len(self.requests)
            self.requests.append(copy.deepcopy(request))
            self.condition.notify_all()
        response = (
            self.responses[index] if index < len(self.responses) else LlmResponse(content="好")
        )
        if callable(response):
            response = response(request)
        if isinstance(response, BaseException):
            raise response
        return response

    def wait(self, count: int) -> LlmRequest:
        with self.condition:
            assert self.condition.wait_for(lambda: len(self.requests) >= count, 3)
        return self.requests[count - 1]


class NeverCompactor(Compactor):
    """压不了。阈值没到的时候压缩本来就不该发生。"""

    def compress(
        self, messages: Sequence[Mapping[str, Any]], *, budget_tokens: int
    ) -> list[dict[str, Any]] | None:
        return None


class Harness:
    def __init__(
        self,
        root: Path,
        *,
        tools: Sequence[Tool] = (),
        policy: PermissionPolicy | None = None,
        responses: Sequence[ScriptedResponse] = (),
        config: AgentConfig | None = None,
        compactor: Compactor | None = None,
        traces: TraceStore | None = None,
        admission_timeout: float = 0.2,
        approval_ttl: float = 10,
        max_concurrency: int = 8,
        queue: EventQueue | None = None,
    ) -> None:
        self.scope = SCOPE
        self.journal = FileJournal(root)
        self.store = RuntimeStore(self.journal)
        # 队列默认是这个 harness 私有的。验「重启后队列里还在」时要把它传进第二个
        # harness —— 内存替身不会像 Redis 那样自己活过进程。
        self.queue = queue or InMemoryEventQueue()
        self.sink = RecordingSink()
        self.config = config or AgentConfig(tool_round_timeout_seconds=0.5, llm_retry_delay=0.01)
        self.details = FileDetailStore(root / "details")
        self.skills = SkillSystem([root / "skills"], scan_dirs=[root / "external-skills"])
        # 内存替身：要问的是「写了什么」，不是「落到磁盘没有」。文件后端另有测试。
        self.traces = traces or RecordingTraceStore()
        # buffer 是落盘的：它就是「已 ACK 但 Brain 还没认领」这件事的存放处，用替身
        # 就测不到重启后还在不在。
        self.buffer = DiskEventBuffer(root / "buffer")
        self.registry = ToolRegistry(default_timeout_seconds=self.config.tool_timeout_seconds)
        self.registry.register(DetailReadTool(self.details))
        self.registry.register(SkillTool(self.skills))
        for tool in tools:
            self.registry.register(tool)
        self.tools = ThreadedToolSystem(
            self.registry,
            self.sink,
            policy or VerdictPolicy(),
            store=self.store,
            queue=self.queue,
            admission_timeout=admission_timeout,
            approval_ttl=approval_ttl,
            max_concurrency=max_concurrency,
            shrinker=ResultShrinker(
                self.details,
                self.config.tool_result_detail_chars,
                whole=frozenset({DETAIL_READ, SKILL}),
            ),
        )
        self.provider = ScriptedProvider(responses)
        self.compactor = compactor or NeverCompactor()
        self.ctx = AgentContext(
            self.journal,
            self.store,
            self.queue,
            self.buffer,
            self.sink,
            self.tools,
            ContextManager(self.journal, self.config),
            self.skills,
            self.traces,
            self.config,
        )
        self.compaction = CompactionCoordinator(
            self.store, self.compactor, self.config, sink=self.sink
        )
        self.brain = AgentBrain(self.ctx, self.provider, self.compaction)
        self.consumers = (
            UserConsumer(self.ctx, self.brain),
            ToolConsumer(self.ctx, self.brain),
            SystemConsumer(self.ctx, self.brain),
        )
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> Harness:
        self.tools.recover_scope(self.scope)
        for consumer in self.consumers:
            thread = threading.Thread(
                target=consumer.run, args=(self.scope, self.stop), daemon=True
            )
            self.threads.append(thread)
            thread.start()
        self.brain.resume(self.scope)
        return self

    def send(self, text: str = "你好", id: str = "u1") -> Inbound:
        return self.submit(Inbound(QueueName.USER, "user.message", {"content": text}, id))

    def decide(
        self, invocation_id: str, status: str = "approved", id: str | None = None
    ) -> Inbound:
        return self.submit(
            Inbound(
                QueueName.SYSTEM,
                "permission.decide",
                {"invocation_id": invocation_id, "status": status},
                id or f"decide:{invocation_id}",
            )
        )

    def submit(self, item: Inbound) -> Inbound:
        """照 producer 的样子投：只进队列。落盘是 Brain 认领时的事。"""
        self.queue.add(self.scope, item)
        return item

    def state(self) -> State:
        return self.store.read(self.scope)

    def calls(self) -> dict[str, Any]:
        calls: dict[str, Any] = self.state()["calls"]
        return calls

    def call(self, name: str) -> Any:
        """一次调用的原始状态条目。没跑过就是 `None` —— 由调用方自己判。"""
        return next((c for c in self.calls().values() if c["tool"] == name), None)

    def batch(self, batch_id: str) -> State:
        """批次的接纳视图。查不到就直接失败 —— 用它的测试都在等一个已登记的批次。"""
        status = self.tools.batch_status(self.scope, batch_id)
        assert status is not None, f"批次 {batch_id} 不存在"
        return status

    def close(self) -> None:
        self.stop.set()
        self.queue.close()
        for thread in self.threads:
            thread.join(2)
        self.buffer.close()
        self.brain.close()
        self.compaction.shutdown()
        self.tools.shutdown()
        self.journal.close()

    def __enter__(self) -> Harness:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def requests(*names: str) -> list[ToolCallRequest]:
    return [ToolCallRequest(name, {}, f"provider-{name}", name, "b1", "r1") for name in names]
