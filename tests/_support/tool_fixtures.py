"""工具系统的测试夹具：假工具、记录型 sink、权限策略、等待工具。

用闸门（`threading.Event` / `Barrier`）而不是 sleep 断言并发：sleep 断言的是
「等了这么久应该跑完了」，闸门断言的是「它们确实同时在跑」。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping

from core.protocol.datatype import (
    AgentEvent,
    Scope,
    ToolDefinition,
    ToolEvent,
    ToolExecution,
    ToolInvocation,
    ToolResult,
)
from core.protocol.datatype.permission import (
    PermissionEvent,
    PermissionRecord,
    PermissionVerdict,
)
from core.protocol.error import PermissionStoreError
from core.protocol.interface import (
    AgentEventSink,
    PermissionPolicy,
    Tool,
    ToolSystem,
)
from core.tool.permission import DEFAULT_TTL_SECONDS, PermissionManager, PermissionStore
from core.utils.encoding import JsonObject

EMPTY_SCHEMA: JsonObject = {"type": "object", "properties": {}}


def definition(
    name: str, schema: JsonObject | None = None, description: str = "测试工具"
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_schema=schema if schema is not None else EMPTY_SCHEMA,
    )


class EchoTool(Tool):
    """立刻返回，把入参原样回显。"""

    def __init__(self, name: str = "echo", schema: JsonObject | None = None) -> None:
        self._definition = definition(name, schema)

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def execute(self, invocation: ToolInvocation) -> ToolResult:
        return ToolResult(content=invocation.arguments)


class RaisingTool(Tool):
    """每次被调用就抛指定异常。用来区分「业务失败」和「意外异常」两条路径。"""

    def __init__(self, name: str, exc: BaseException) -> None:
        self._definition = definition(name)
        self.exc = exc

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def execute(self, invocation: ToolInvocation) -> ToolResult:
        raise self.exc


class StaticTool(Tool):
    """定义完全由测试指定。用来构造不合规的定义。"""

    def __init__(self, tool_definition: ToolDefinition) -> None:
        self._definition = tool_definition

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def execute(self, invocation: ToolInvocation) -> ToolResult:
        return ToolResult(content=None)


class GatedTool(Tool):
    """卡在闸门上，直到测试放行。

    `entered` 每进一个就 +1，`release` 放行全部。用来断言「execute 返回时还没跑完」
    和「一批里的调用同时在跑」。
    """

    def __init__(
        self,
        name: str = "gated",
        gate: threading.Event | None = None,
        barrier: threading.Barrier | None = None,
        body: Callable[[ToolInvocation], ToolResult] | None = None,
        schema: JsonObject | None = None,
    ) -> None:
        self._definition = definition(name, schema)
        self.gate = gate if gate is not None else threading.Event()
        self.barrier = barrier
        self.body = body
        self.entered = 0
        self.finished: list[str] = []
        self._lock = threading.Lock()

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def execute(self, invocation: ToolInvocation) -> ToolResult:
        with self._lock:
            self.entered += 1
        if self.barrier is not None:
            # 全部并发才到得了这里；串行会直接 BrokenBarrierError。
            self.barrier.wait(timeout=10)
        self.gate.wait(timeout=10)
        with self._lock:
            self.finished.append(invocation.invocation_id)
        if self.body is not None:
            return self.body(invocation)
        return ToolResult(content="done")


def _matching(events: list[AgentEvent], type_: str) -> Iterator[PermissionEvent]:
    """筛出某类审批事件。不持锁 —— 调用方要么已经持着 `_lock`，要么只读快照。"""
    return (event for event in events if isinstance(event, PermissionEvent) and event.type == type_)


def _tool_events(events: list[AgentEvent]) -> Iterator[ToolEvent]:
    """筛出终态事件。同样不持锁：`Condition` 与 `_lock` 是同一把，不可重入。"""
    return (event for event in events if isinstance(event, ToolEvent))


class RecordingSink(AgentEventSink):
    """记录全部事件（终态与审批事件走同一个口），可选在投递时抛错。

    事件是跨线程投递的，所以等待靠闸门（`Condition`）而不是轮询 —— 等的是
    「事件真的到了」这个事实，不是一个时长猜出来的窗口。
    """

    def __init__(self, fail: bool = False) -> None:
        self.events: list[AgentEvent] = []
        self.fail = fail
        self._lock = threading.Lock()
        self._arrived = threading.Condition(self._lock)

    def emit(self, event: AgentEvent) -> None:
        if self.fail:
            raise RuntimeError("投递失败（测试）")
        with self._arrived:
            self.events.append(event)
            self._arrived.notify_all()

    def wait_for(self, count: int, timeout: float = 10.0) -> list[AgentEvent]:
        """等到攒够 `count` 条事件（不限类型）。返回当时的快照。"""
        with self._arrived:
            ok = self._arrived.wait_for(lambda: len(self.events) >= count, timeout=timeout)
        if not ok:
            raise AssertionError(f"等待 {count} 条事件超时，只到了 {len(self.events)} 条")
        with self._lock:
            return list(self.events)

    def wait_for_tool(self, count: int, timeout: float = 10.0) -> list[ToolEvent]:
        """等到攒够 `count` 条终态事件。返回当时的快照。"""
        with self._arrived:
            ok = self._arrived.wait_for(
                lambda: sum(1 for _ in _tool_events(self.events)) >= count, timeout=timeout
            )
        if not ok:
            raise AssertionError(
                f"等待 {count} 条终态事件超时，只到了 {len(self.tool_events())} 条"
            )
        return self.tool_events()

    def wait_for_permission(
        self, type_: str, count: int = 1, timeout: float = 5.0
    ) -> list[PermissionEvent]:
        """等到攒够 `count` 条某类型的审批事件。返回当时的快照。"""
        with self._arrived:
            ok = self._arrived.wait_for(
                lambda: sum(1 for _ in _matching(self.events, type_)) >= count,
                timeout=timeout,
            )
        if not ok:
            raise AssertionError(f"等 {count} 条 {type_} 事件超时")
        return self.permission_of_type(type_)

    def permission_of_type(self, type_: str) -> list[PermissionEvent]:
        with self._lock:
            return list(_matching(self.events, type_))

    def tool_events(self) -> list[ToolEvent]:
        with self._lock:
            return list(_tool_events(self.events))

    def permission_events(self) -> list[PermissionEvent]:
        with self._lock:
            return [event for event in self.events if isinstance(event, PermissionEvent)]

    def outcomes(self) -> dict[str, str]:
        with self._lock:
            return {event.invocation_id: event.outcome for event in _tool_events(self.events)}


class MemoryPermissionStore(PermissionStore):
    """内存实现。测试大部分只需要「记下来就行」，落盘语义由专门的套件覆盖。"""

    def __init__(self) -> None:
        self._current: dict[tuple[str, str, str], PermissionRecord] = {}
        self._lock = threading.Lock()

    def record(self, record: PermissionRecord) -> None:
        with self._lock:
            key = _record_key(record)
            if key in self._current:
                raise PermissionStoreError(f"{key} 已有权限记录")
            self._current[key] = record

    def resolve(self, record: PermissionRecord) -> bool:
        with self._lock:
            key = _record_key(record)
            known = self._current.get(key)
            if known is None or known.is_resolved:
                return False
            self._current[key] = record
            return True

    def get(self, scope: Scope, invocation_id: str) -> PermissionRecord | None:
        with self._lock:
            return self._current.get((scope.user_id, scope.session_id, invocation_id))

    def pending(self, scope: Scope) -> tuple[PermissionRecord, ...]:
        with self._lock:
            return tuple(
                record
                for record in self._current.values()
                if record.scope == scope and not record.is_resolved
            )


def _record_key(record: PermissionRecord) -> tuple[str, str, str]:
    return (record.scope.user_id, record.scope.session_id, record.invocation_id)


class VerdictPolicy(PermissionPolicy):
    """按工具名给判定值，默认值给没列出的工具。记录每次被问过什么。"""

    def __init__(
        self,
        verdicts: Mapping[str, str] | None = None,
        *,
        default: str = PermissionVerdict.ALLOW,
    ) -> None:
        self.verdicts = dict(verdicts or {})
        self.default = default
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def check(self, scope: Scope, tool: str, arguments: JsonObject) -> str:
        with self._lock:
            self.calls.append((scope.user_id, tool))
        return self.verdicts.get(tool, self.default)


class BrokenPolicy(PermissionPolicy):
    """策略实现自己抛错。用来验证 core 是 fail closed 而不是 fail open。"""

    def check(self, scope: Scope, tool: str, arguments: JsonObject) -> str:
        raise RuntimeError("策略服务连不上（测试）")


def permission_manager(
    policy: PermissionPolicy | None = None,
    *,
    store: PermissionStore | None = None,
    sink: AgentEventSink | None = None,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> PermissionManager:
    """装配一个 `PermissionManager`。默认内存存储 + 记录型 sink。"""
    return PermissionManager(
        store if store is not None else MemoryPermissionStore(),
        policy if policy is not None else VerdictPolicy(),
        sink if sink is not None else RecordingSink(),
        ttl_seconds=ttl_seconds,
    )


def deny(tools: frozenset[str] | set[str]) -> VerdictPolicy:
    """一批按名拒绝的工具。"""
    return VerdictPolicy({tool: PermissionVerdict.DENY for tool in tools})


def wait_terminal(
    system: ToolSystem, scope: Scope, invocation_id: str, timeout: float = 5.0
) -> ToolExecution:
    """等一次执行到达终态。轮询执行表 —— 这是唯一能观察后台线程的方式。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        execution = system.status(scope, invocation_id)
        if execution is not None and execution.is_terminal:
            return execution
        time.sleep(0.002)
    raise AssertionError(f"{invocation_id} 在 {timeout}s 内没有到达终态")


def wait_entered(tool: GatedTool, count: int, timeout: float = 5.0) -> None:
    """等工具被线程取走 `count` 次。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tool.entered >= count:
            return
        time.sleep(0.002)
    raise AssertionError(f"工具只被取走了 {tool.entered} 次，没到 {count} 次")


__all__ = [
    "EMPTY_SCHEMA",
    "BrokenPolicy",
    "EchoTool",
    "GatedTool",
    "MemoryPermissionStore",
    "RaisingTool",
    "RecordingSink",
    "StaticTool",
    "VerdictPolicy",
    "definition",
    "deny",
    "permission_manager",
    "wait_entered",
    "wait_terminal",
]
