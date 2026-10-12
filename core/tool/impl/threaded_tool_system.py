"""线程池驱动的工具系统实现。

三件事：

1. **立即返回的批量派发**。同一批里的调用互不依赖，全部并行排队；`execute` 返回
   时没有一个工具跑完。
2. **超时 = core 不再等待**，不是终止线程。Python 没有安全的线程终止手段，强杀会让
   `finally` 不跑、锁不释放、连接不关。所以到点的是 watchdog：它把这次执行判成
   `TIMEOUT` 并发终态事件，工具线程随后返回时看到已有终态，**丢弃结果**。
   终态一旦落下就不可改写，所以「超时后结果姗姗来迟」不会把 `TIMEOUT` 变成
   `SUCCEEDED`。
3. **每个被接纳的调用恰好一条终态事件**。投递失败把状态改成 `OUTCOME_UNKNOWN` ——
   工具确实跑过了，只是结果没送出去，不能记成「没跑过」让上层去重试一个可能已经
   生效的副作用。
"""

from __future__ import annotations

import heapq
import itertools
import threading
import time
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Final

from jsonschema import validate as json_schema_validate
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from core.protocol.datatype import (
    MAX_INVOCATION_ID_LENGTH,
    RESULT_KEY_PREFIX,
    BatchExecution,
    Scope,
    ToolCallRequest,
    ToolDefinition,
    ToolErrorInfo,
    ToolErrorKind,
    ToolEvent,
    ToolEventType,
    ToolExecution,
    ToolHandle,
    ToolHandleState,
    ToolInvocation,
    ToolOutcome,
    ToolResult,
    ToolStatus,
    is_valid_invocation_id,
)
from core.protocol.datatype.permission import PermissionVerdict
from core.protocol.error import (
    PermissionDeniedError,
    PermissionError,
    ToolError,
    ToolExecutionError,
    ToolRequestError,
)
from core.protocol.interface import ToolEventSink, ToolSystem
from core.tool.permission import PermissionManager
from core.tool.registry import RegisteredTool, ToolRegistry
from core.utils.encoding import JsonObject, utc_now_iso

#: 线程池大小。agent 的并行工具调用典型是个位数，8 路给足并发又不至于让一个会话
#: 的几十个工具把池子吃满。
DEFAULT_MAX_CONCURRENCY: Final = 8

#: 执行中与排队中合计的上限。留 4 倍余量吸收突发批次。再多的调用当场被拒 —— 立即返回的
#: 接口不能阻塞，默默排到无限长会让一个失控的模型请求把内存吃光。
DEFAULT_MAX_PENDING: Final = 32

#: 已终结的执行记录保留条数。执行表是内存派生设施，不设上限就是长期运行进程里的
#: 一处慢性泄漏。超出后按接纳顺序淘汰最老的终态记录。
DEFAULT_MAX_HISTORY: Final = 1024

#: 错误消息进事件 payload 前的截断长度。完整 traceback 和超长消息不该进 journal ——
#: 既撑大日志，也容易把内部细节带到客户端视图里。
MAX_ERROR_MESSAGE_LENGTH: Final = 500

#: 权限判定为 ask 时给模型看的那句话。走 `content` 而不是让投影把整个 payload
#: 序列化过去 —— 模型读到一段结构化 JSON 只会困惑，读到一句话才知道要等。
AWAITING_APPROVAL_CONTENT: Final = "这个工具调用正在等待人工批准，批准后才会执行。"


def _message(exc: BaseException) -> str:
    """异常的消息文本。刻意不带 traceback。

    traceback 是调试信息不是事件内容：它会把源码结构、内部路径和第三方库的
    报错细节一路带到客户端的 trace 视图里。
    """
    text = f"{type(exc).__name__}: {exc}"
    if len(text) > MAX_ERROR_MESSAGE_LENGTH:
        return text[:MAX_ERROR_MESSAGE_LENGTH] + "…"
    return text


@dataclass(slots=True)
class _Cell:
    """执行表的一格。可变：状态迁移在这上面发生。"""

    seq: int
    execution: ToolExecution
    registered: RegisteredTool | None
    deadline: float
    timeout_seconds: float


@dataclass(order=True, slots=True)
class _Deadline:
    """watchdog 的堆项。按 `(deadline, seq)` 排序，seq 用来打破同刻的平局。"""

    deadline: float
    seq: int = field(compare=True)
    cell: _Cell = field(compare=False)


class ThreadedToolSystem(ToolSystem):
    """`ToolSystem` 的线程池实现。

    线程安全：`_lock` 保护执行表与接纳计数；事件投递在锁外做 —— sink 可能慢，
    甚至可能回调回本对象。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        sink: ToolEventSink,
        permission: PermissionManager,
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        max_pending: int = DEFAULT_MAX_PENDING,
        max_history: int = DEFAULT_MAX_HISTORY,
    ) -> None:
        # 权限管理器没有默认值：默认放行等于把「每次执行前检查」变成一个默认关闭的
        # 开关，靠记得传参来保证安全性。
        self._registry = registry
        self._sink = sink
        self._permission = permission
        self._max_pending = _positive(max_pending, "max_pending")
        self._max_history = _positive(max_history, "max_history")

        self._pool = ThreadPoolExecutor(
            max_workers=_positive(max_concurrency, "max_concurrency"),
            thread_name_prefix="oath-tool",
        )
        self._lock = threading.Lock()
        self._table: dict[tuple[str, str, str], _Cell] = {}
        self._heap: list[_Deadline] = []
        # 执行中（in-flight）且尚未终结的调用数。名额按它算，不按执行表大小算 ——
        # 已终结的记录留着给 `status()` 查，但不占名额。
        self._in_flight = 0
        self._counter = itertools.count()
        self._stopped = False

        self._condition = threading.Condition(self._lock)
        self._watchdog = threading.Thread(
            target=self._watch_loop, name="oath-tool-watchdog", daemon=True
        )
        self._watchdog.start()

    # ---------------------------------------------------------------- 查询

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._registry.definitions()

    def status(self, scope: Scope, invocation_id: str) -> ToolExecution | None:
        with self._lock:
            cell = self._table.get(_key(scope, invocation_id))
            return None if cell is None else cell.execution

    # ---------------------------------------------------------------- 派发

    def execute(self, scope: Scope, requests: Sequence[ToolCallRequest]) -> BatchExecution:
        """派发一批调用，立即返回。

        接纳检查的顺序是固定的：**去重 → 工具存在 → 权限 → 入参 → 名额**。
        先便宜的、再判定性的，最后才占稀缺资源。权限排在入参前面：权限问的是
        「这个主体能不能调这个工具」，拿不合法参数去问没有意义，而且可能把参数内容
        泄给不该看它的策略实现。
        """
        if self._stopped:
            raise ToolRequestError("工具系统已关闭")
        if not requests:
            raise ToolRequestError("批次为空")
        handles = [self._admit(scope, request) for request in requests]
        return BatchExecution(handles=tuple(handles))

    def shutdown(self, *, wait: bool = True) -> None:
        with self._condition:
            if self._stopped:
                return
            self._stopped = True
            self._condition.notify_all()
        self._pool.shutdown(wait=wait, cancel_futures=False)

    def _admit(self, scope: Scope, request: ToolCallRequest) -> ToolHandle:
        if not isinstance(request, ToolCallRequest):
            raise ToolRequestError(f"request 必须是 ToolCallRequest: {type(request)!r}")
        if not isinstance(request.arguments, dict):
            raise ToolRequestError(f"arguments 必须是 JSON 对象: {type(request.arguments)!r}")
        invocation_id = request.invocation_id or uuid.uuid4().hex
        if not is_valid_invocation_id(invocation_id):
            raise ToolRequestError(
                f"invocation_id 长度必须在 1..{MAX_INVOCATION_ID_LENGTH} 之间（它要拼进幂等键）: "
                f"{invocation_id!r}"
            )

        with self._lock:
            existing = self._table.get(_key(scope, invocation_id))
            if existing is not None:
                # 正在执行或已终结。已终结的再次派发同样不重跑：重试策略不属于工具
                # 系统，它是上层的决定。
                return ToolHandle(
                    invocation_id=invocation_id,
                    tool=request.tool,
                    state=ToolHandleState.DEDUPLICATED,
                )
        return self._accept(scope, request, invocation_id)

    def _accept(self, scope: Scope, request: ToolCallRequest, invocation_id: str) -> ToolHandle:
        registered = self._registry.get(request.tool)
        if registered is None:
            return self._reject(
                scope,
                invocation_id,
                request,
                ToolErrorKind.UNKNOWN_TOOL,
                f"工具未注册: {request.tool!r}",
            )

        try:
            verdict = self._permission.authorize(
                scope,
                registered.name,
                request.tool_call_id,
                invocation_id,
                request.arguments,
            )
        except PermissionDeniedError as exc:
            return self._reject(
                scope,
                invocation_id,
                request,
                ToolErrorKind.PERMISSION_DENIED,
                _message(exc),
            )
        except PermissionError as exc:
            # 权限这一步**没能完成**（存储故障、策略返回了没约定的判定）。它和
            # 「判定为拒绝」是两件事，kind 分开是为了让上层能区分「换个工具再试」
            # 与「系统本身出问题了」。
            #
            # 折成当场拒绝而不是往外抛：一批里的每一项独立。一项问不出结果就把
            # 整批的 handle 丢掉是最差的结果 —— 其他项已经接纳、已经在跑，调用方
            # 却拿不到它们的 id。
            return self._reject(
                scope,
                invocation_id,
                request,
                ToolErrorKind.PERMISSION_ERROR,
                _message(exc),
            )
        if verdict == PermissionVerdict.DENY:
            return self._reject(
                scope,
                invocation_id,
                request,
                ToolErrorKind.PERMISSION_DENIED,
                "权限判定为拒绝",
            )
        if verdict == PermissionVerdict.ASK:
            return self._await_approval(scope, invocation_id, request, registered.name)

        try:
            json_schema_validate(
                instance=request.arguments, schema=registered.definition.input_schema
            )
        except JsonSchemaValidationError as exc:
            return self._reject(
                scope,
                invocation_id,
                request,
                ToolErrorKind.INVALID_ARGUMENTS,
                _truncate(exc.message),
            )

        now = utc_now_iso()
        execution = ToolExecution(
            scope=scope,
            invocation_id=invocation_id,
            tool_call_id=request.tool_call_id,
            tool=registered.name,
            arguments=request.arguments,
            status=ToolStatus.PENDING,
            result=None,
            error=None,
            submitted_dt=now,
        )
        cell = _Cell(
            seq=next(self._counter),
            execution=execution,
            registered=registered,
            deadline=time.monotonic() + registered.timeout_seconds,
            timeout_seconds=registered.timeout_seconds,
        )

        admitted = False
        with self._condition:
            if self._in_flight < self._max_pending:
                self._table[_key(scope, invocation_id)] = cell
                self._in_flight += 1
                self._evict_locked()
                heapq.heappush(self._heap, _Deadline(cell.deadline, cell.seq, cell))
                self._condition.notify_all()
                admitted = True
        if not admitted:
            # 名额满了。已经在跑的调用不受影响 —— 一批里的每一项独立。拒绝路径走锁外，
            # 因为它要发事件，而 sink 可能回调回本对象。
            return self._reject(
                scope,
                invocation_id,
                request,
                ToolErrorKind.BUSY,
                f"执行中与排队已达上限 {self._max_pending}",
            )

        self._pool.submit(self._run, cell)
        return ToolHandle(
            invocation_id=invocation_id, tool=registered.name, state=ToolHandleState.ACCEPTED
        )

    def _await_approval(
        self,
        scope: Scope,
        invocation_id: str,
        request: ToolCallRequest,
        tool: str,
    ) -> ToolHandle:
        """权限判定是 ask：不跑，但**当场把模型那个槽位填上**。

        「正在等待批准」这条 tool result 就是对话历史的一部分，不是临时拼的
        占位符 —— context 是 append-only 的，审批回来之后不会再有第二条结果
        覆盖它。LLM 的 message 数组里每个 tool call 必须配一条 tool result，
        缺一条这个数组交给任何 provider 都是坏的。

        不进执行表：没有任何东西被执行过，「这个 id 跑成什么」暂时无从谈起。
        审批状态查 `PermissionManager`，不查这里。
        """
        now = utc_now_iso()
        cell = _Cell(
            seq=next(self._counter),
            execution=ToolExecution(
                scope=scope,
                invocation_id=invocation_id,
                tool_call_id=request.tool_call_id,
                tool=tool,
                arguments=request.arguments,
                status=ToolStatus.AWAITING_APPROVAL,
                result=ToolResult(content=AWAITING_APPROVAL_CONTENT),
                error=None,
                submitted_dt=now,
                finished_dt=now,
            ),
            registered=None,
            deadline=0.0,
            timeout_seconds=0.0,
        )
        self._emit(cell)
        return ToolHandle(
            invocation_id=invocation_id, tool=tool, state=ToolHandleState.PENDING_APPROVAL
        )

    def _reject(
        self,
        scope: Scope,
        invocation_id: str,
        request: ToolCallRequest,
        kind: str,
        message: str,
    ) -> ToolHandle:
        """当场拒绝。发终态事件，但**不进执行表**。

        没有东西被执行过，执行表里留着一条记录只会让「这个 id 跑过什么」和
        「这个 id 跑成了什么」混为一谈。拒绝原因看返回值和终态事件。
        """
        error = ToolErrorInfo(kind=kind, message=message)
        now = utc_now_iso()
        cell = _Cell(
            seq=next(self._counter),
            execution=ToolExecution(
                scope=scope,
                invocation_id=invocation_id,
                tool_call_id=request.tool_call_id,
                tool=request.tool,
                arguments=request.arguments,
                status=ToolStatus.FAILED,
                result=None,
                error=error,
                submitted_dt=now,
                finished_dt=now,
            ),
            registered=None,
            deadline=0.0,
            timeout_seconds=0.0,
        )
        self._emit(cell)
        return ToolHandle(
            invocation_id=invocation_id,
            tool=request.tool,
            state=ToolHandleState.REJECTED,
            error=error,
        )

    def _evict_locked(self) -> None:
        """超出历史保留条数后，淘汰接纳顺序最老的**终态**记录。

        执行中的绝不淘汰 —— 那样 `status()` 会在执行途中突然查不到。
        """
        if len(self._table) <= self._max_history:
            return
        stale = sorted(
            (cell for cell in self._table.values() if cell.execution.is_terminal),
            key=lambda cell: cell.seq,
        )
        for cell in stale[: len(self._table) - self._max_history]:
            del self._table[_key(cell.execution.scope, cell.execution.invocation_id)]

    # ---------------------------------------------------------------- 执行

    def _run(self, cell: _Cell) -> None:
        registered = cell.registered
        assert registered is not None  # 只有被接纳的项会进这里
        with self._lock:
            if cell.execution.is_terminal:
                # 到点被 watchdog 判超时后才被 worker 取到的排队项：直接跳过，
                # 绝不执行一个已经有终态的调用。
                return
            cell.execution = replace(
                cell.execution, status=ToolStatus.RUNNING, started_dt=utc_now_iso()
            )
        invocation = ToolInvocation(
            scope=cell.execution.scope,
            invocation_id=cell.execution.invocation_id,
            arguments=cell.execution.arguments,
            deadline=cell.deadline,
        )
        try:
            result = registered.tool.execute(invocation)
        except ToolExecutionError as exc:
            self._settle(
                cell, ToolOutcome.FAILED, ToolErrorInfo(ToolErrorKind.TOOL_ERROR, _message(exc))
            )
        except Exception as exc:  # 业务异常是结果，不是崩溃
            self._settle(
                cell, ToolOutcome.FAILED, ToolErrorInfo(ToolErrorKind.UNEXPECTED, _message(exc))
            )
        else:
            self._settle(cell, ToolOutcome.SUCCEEDED, None, result=result)

    def _settle(
        self,
        cell: _Cell,
        outcome: str,
        error: ToolErrorInfo | None,
        *,
        result: ToolResult | None = None,
    ) -> None:
        """落终态并发事件。已有终态则整段丢弃。

        迟到结果的丢弃就在这里发生：watchdog 已经判过超时，这次 `settle` 什么也
        不做，终态不可被改写。
        """
        with self._condition:
            if cell.execution.is_terminal:
                return
            cell.execution = replace(
                cell.execution,
                status=outcome,
                result=result,
                error=error,
                finished_dt=utc_now_iso(),
            )
            self._in_flight -= 1
            self._condition.notify_all()
        self._emit(cell)

    # ---------------------------------------------------------------- 超时

    def _watch_loop(self) -> None:
        """到点就不等。不终止线程 —— 见模块开头。"""
        while True:
            with self._condition:
                while True:
                    self._prune_locked()
                    if self._stopped:
                        return
                    if not self._heap:
                        self._condition.wait()
                        continue
                    remaining = self._heap[0].deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
                expired = heapq.heappop(self._heap)
            self._settle(
                expired.cell,
                ToolOutcome.TIMEOUT,
                ToolErrorInfo(
                    ToolErrorKind.TIMEOUT,
                    f"core 在 {expired.cell.timeout_seconds:g}s 内未等到结果，不再等待",
                ),
            )

    def _prune_locked(self) -> None:
        """清掉堆里已终结的项。

        正常完成的项不会自己从堆里消失，不清就是一个只增不减的集合 —— 长期运行的
        进程里这就是一处慢性泄漏。每次醒来都重建一次，堆因此只装着执行中的项。
        """
        if not self._heap:
            return
        self._heap = [item for item in self._heap if not item.cell.execution.is_terminal]
        heapq.heapify(self._heap)

    # ---------------------------------------------------------------- 事件

    def _emit(self, cell: _Cell) -> None:
        """投递终态事件。投递失败 → `OUTCOME_UNKNOWN`。

        `outcome` 只可能是 `succeeded` / `failed` / `timeout` / `awaiting_approval`：
        `outcome_unknown` 的含义正是「这条事件没送出去」，它出现在状态里，不会出现在
        事件里。下游看到某个调用没有终态事件，那就是信号本身。
        """
        execution = cell.execution
        payload: JsonObject = {
            "tool_call_id": execution.tool_call_id,
            "invocation_id": execution.invocation_id,
            "outcome": execution.status,
        }
        if execution.result is not None:
            payload["content"] = execution.result.content
        elif execution.error is not None:
            payload["error"] = execution.error.to_payload()
        event = ToolEvent(
            type=ToolEventType.TERMINAL,
            scope=execution.scope,
            tool_call_id=execution.tool_call_id,
            invocation_id=execution.invocation_id,
            tool=execution.tool,
            outcome=execution.status,
            payload=payload,
            idempotency_key=f"{RESULT_KEY_PREFIX}{execution.invocation_id}",
            occur_dt=execution.finished_dt or utc_now_iso(),
        )
        try:
            self._sink.emit(event)
        except Exception as exc:  # sink 的失败方式无法预判
            self._mark_unknown(cell, exc)

    def _mark_unknown(self, cell: _Cell, exc: BaseException) -> None:
        """事件没送出去：结果存在过，但丢了。

        不重试投递 —— 送不出去的时候重试只会反复失败，而且会把这次执行的终态
        拖到不确定的时刻。这条执行的结果由上层对账 journal 处理。
        """
        with self._lock:
            cell.execution = replace(
                cell.execution,
                status=ToolStatus.OUTCOME_UNKNOWN,
                error=ToolErrorInfo(ToolErrorKind.SINK_ERROR, _message(exc)),
            )


def _key(scope: Scope, invocation_id: str) -> tuple[str, str, str]:
    return (scope.user_id, scope.session_id, invocation_id)


def _positive(value: int, name: str) -> int:
    if value <= 0:
        raise ToolError(f"{name} 必须为正: {value!r}")
    return value


def _truncate(text: str) -> str:
    if len(text) > MAX_ERROR_MESSAGE_LENGTH:
        return text[:MAX_ERROR_MESSAGE_LENGTH] + "…"
    return text


__all__ = [
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_MAX_HISTORY",
    "DEFAULT_MAX_PENDING",
    "MAX_ERROR_MESSAGE_LENGTH",
    "ThreadedToolSystem",
]
