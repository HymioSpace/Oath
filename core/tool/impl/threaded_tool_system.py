"""完整批次接纳、持久化执行资格与独立工具 worker。

所有资格/终态在 RuntimeStore 中原子提交。工具与权限策略都在锁外运行；
审批只恢复原 invocation。运行过但无法确认结果的调用在重启时报告 unknown。
"""

from __future__ import annotations

import copy
import hashlib
import logging
import math
import threading
import time
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from jsonschema import validate

from core.detail.shrink import ResultShrinker
from core.journal.runtime_store import RuntimeStore, State
from core.observability import TOOL_DURATION, TOOL_TERMINALS
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import QueueName
from core.protocol.datatype.tool import (
    BatchExecution,
    ToolCallRequest,
    ToolDefinition,
    ToolErrorInfo,
    ToolExecution,
    ToolHandle,
    ToolHandleState,
    ToolInvocation,
    ToolResult,
    is_valid_invocation_id,
)
from core.protocol.datatype.trace import Span, SpanKind, SpanStatus
from core.protocol.datatype.user import User
from core.protocol.error import ToolExecutionError, ToolRequestError
from core.protocol.interface import AgentEventSink, EventQueue, PermissionPolicy, ToolSystem
from core.protocol.interface.trace import TraceStore
from core.tool.lifecycle import TERMINAL
from core.tool.lifecycle import terminal as _terminal
from core.tool.permission import PermissionManager
from core.tool.registry import ToolRegistry
from core.trace.attributes import (
    OATH_CHANNEL,
    OATH_STEP_ID,
    OATH_TOOL_DURATION_MS,
    OATH_TOOL_NAME,
    OATH_TOOL_OUTCOME,
    OATH_TOOL_QUEUE_MS,
)
from core.utils.deadlines import Deadlines
from core.utils.encoding import canonical_json, utc_now_iso

_log = logging.getLogger(__name__)
DEFAULT_MAX_CONCURRENCY = 8
DEFAULT_MAX_PENDING = 32
MAX_ERROR_MESSAGE_LENGTH = 500


def _elapsed_ms(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        started = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return round(max(0, (time.time() - started) * 1000), 3)


class ThreadedToolSystem(ToolSystem):
    def __init__(
        self,
        registry: ToolRegistry,
        sink: AgentEventSink,
        policy: PermissionPolicy,
        *,
        store: RuntimeStore,
        queue: EventQueue,
        traces: TraceStore,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        max_pending: int = DEFAULT_MAX_PENDING,
        approval_ttl: float = 300,
        admission_timeout: float = 5,
        shrinker: ResultShrinker | None = None,
    ) -> None:
        for name, value in (
            ("max_concurrency", max_concurrency),
            ("max_pending", max_pending),
            ("approval_ttl", approval_ttl),
            ("admission_timeout", admission_timeout),
        ):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须是有限正数")
        if not isinstance(max_concurrency, int) or not isinstance(max_pending, int):
            raise ValueError("执行和接纳容量必须是正整数")
        self.store, self._queue, self._traces = store, queue, traces
        self._registry, self._sink = registry, sink
        self._shrinker = shrinker
        self._max_pending, self._ttl, self._admission_timeout = (
            max_pending,
            approval_ttl,
            admission_timeout,
        )
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._policy_slots = threading.BoundedSemaphore(max_pending)
        self._lock = threading.Lock()
        self._scheduled: set[tuple[User, str]] = set()
        self._stop = threading.Event()
        self._deadlines = Deadlines()
        self.permissions = PermissionManager(store, policy, self._deadlines, approval_ttl)
        self._watchdog = threading.Thread(
            target=self._watch, name="oath-tool-watchdog", daemon=True
        )
        self._watchdog.start()

    def definitions(
        self, *, delegated=False, channel: str | None = None
    ) -> tuple[ToolDefinition, ...]:
        return self._registry.definitions(delegated=delegated, channel=channel)

    # -- 终态发布 ------------------------------------------------------------

    def _collect(
        self, out: list[Inbound], state: State, call: State, outcome: str, **kw: Any
    ) -> None:
        """在事务里终结一次调用，把要发布的终态攒到 `out`。"""
        item = _terminal(state, call, outcome, **kw)
        if item is not None:
            out.append(item)
            TOOL_TERMINALS.labels(call["tool"], outcome).inc()

    def _publish(self, user: User, items: list[Inbound]) -> None:
        # Tool terminals and their outbound events are committed by Storage atomically.
        for item in items:
            if item.kind == QueueName.SYSTEM:
                self._queue.add(user, item)

    def execute(self, user: User, requests: Sequence[ToolCallRequest]) -> BatchExecution:
        ids = [r.invocation_id or uuid.uuid4().hex for r in requests]
        batch_id = (
            next((r.batch_id for r in requests if r.batch_id), "")
            or (hashlib.sha256(canonical_json(ids).encode()).hexdigest()[:24])
        )
        calls = [
            ToolCallRequest(
                r.tool, copy.deepcopy(r.arguments), r.tool_call_id, i, batch_id, r.run_id
            )
            for r, i in zip(requests, ids, strict=True)
        ]
        return self.submit_batch(user, batch_id, calls)

    def submit_batch(
        self, user: User, batch_id: str, requests: Sequence[ToolCallRequest]
    ) -> BatchExecution:
        if self._stop.is_set():
            raise ToolRequestError("工具系统已关闭")
        ids = [r.invocation_id for r in requests]
        tool_ids = [r.tool_call_id for r in requests]
        if (
            not batch_id
            or not requests
            or len(set(ids)) != len(ids)
            or len(set(tool_ids)) != len(tool_ids)
            or any(not i or not is_valid_invocation_id(i) for i in ids)
            or any(not isinstance(r.arguments, dict) or not r.tool_call_id for r in requests)
        ):
            raise ToolRequestError("完整批次要求非空且唯一的 invocation_id / tool_call_id")
        manifest = [
            {
                "invocation_id": r.invocation_id,
                "tool_call_id": r.tool_call_id,
                "tool": r.tool,
                "arguments": copy.deepcopy(r.arguments),
                "batch_id": batch_id,
                "run_id": r.run_id,
                "channel": r.channel,
            }
            for r in requests
        ]

        def declare(state: State) -> bool:
            known = state["batches"].get(batch_id)
            if known:
                if known["manifest"] != manifest:
                    raise ToolRequestError("batch_id 内容冲突")
                return False
            if any(i in state["calls"] for i in ids):
                raise ToolRequestError("invocation_id 已属于其他批次")
            now = time.time()
            state["batches"][batch_id] = {
                "manifest": manifest,
                "members": ids,
                "initial_submitted": [],
                "admission_deadline": now + self._admission_timeout,
                "admitted": False,
                "presented_by_step": None,
                #: 这一批的真结果进过模型的上下文。占位符不算。
                "delivered": False,
            }
            for item in manifest:
                state["calls"][item["invocation_id"]] = {
                    **item,
                    "status": "admitting",
                    "source": "brain",
                    "admission": None,
                    "terminal": None,
                    "permission": None,
                    "submitted_dt": utc_now_iso(),
                    "started_dt": None,
                    "finished_dt": None,
                }
            return True

        fresh = self.store.transact(user, "tool.batch_declared", declare)
        if fresh:
            for item in manifest:
                invocation_id = str(item["invocation_id"])
                if self._policy_slots.acquire(blocking=False):
                    threading.Thread(
                        target=self._classify,
                        args=(user, invocation_id),
                        name="oath-tool-admission",
                        daemon=True,
                    ).start()
                else:
                    self._classified(
                        user, invocation_id, {"verdict": "deny", "error": "接纳容量已满"}
                    )
        return BatchExecution(
            handles=tuple(
                ToolHandle(
                    invocation_id=str(i),
                    tool=r.tool,
                    state=ToolHandleState.ACCEPTED if fresh else ToolHandleState.DEDUPLICATED,
                )
                for i, r in zip(ids, requests, strict=True)
            )
        )

    def _classify(self, user: User, invocation_id: str) -> None:
        try:
            call = self.store.entries(user, "calls", [invocation_id])[invocation_id]
            registered = self._registry.get(call["tool"])
            if registered is None:
                raise ValueError("工具未注册")
            if user.agent_id and not registered.tool.allow_subagent:
                raise ValueError("Tool is unavailable to delegated executions")
            if not user.agent_id and registered.tool.subagent_only:
                raise ValueError("Tool is only available to delegated executions")
            channel = call.get("channel", "unknown")
            if (
                registered.tool.expose_channels is not None
                and channel not in registered.tool.expose_channels
            ):
                raise ValueError("Tool is unavailable on this client channel")
            verdict = self.permissions.authorize(
                user, call["tool"], copy.deepcopy(call["arguments"])
            )
            if user.agent_id and verdict == "ask":
                verdict = "deny"
            if verdict not in {"allow", "ask", "deny"}:
                raise ValueError("未知权限判定")
            if verdict != "deny":
                validate(call["arguments"], registered.definition.input_schema)
            admission = {
                "verdict": verdict,
                "timeout": registered.timeout_seconds,
                "definition": registered.definition.to_dict(),
            }
            self._classified(user, invocation_id, admission)
        except Exception as exc:
            self._classified(
                user,
                invocation_id,
                {"verdict": "deny", "error": str(exc)[:MAX_ERROR_MESSAGE_LENGTH]},
            )
        finally:
            self._policy_slots.release()

    def _classified(self, user: User, invocation_id: str, admission: State) -> None:
        if self._stop.is_set():
            return

        def change(state: State) -> None:
            call = state["calls"][invocation_id]
            if call["status"] == "admitting" and call["admission"] is None:
                call["admission"] = admission

        self.store.transact(user, "tool.classified", change)
        self.maintain(user)

    def _activate(self, user: User, state: State, now: float, out: list[Inbound]) -> None:
        for batch in state["batches"].values():
            if batch["admitted"]:
                continue
            calls = [state["calls"][i] for i in batch["members"]]
            if (
                any(c["admission"] is None and c["status"] not in TERMINAL for c in calls)
                and self._deadlines.remaining(
                    (user, calls[0]["batch_id"], "admission"),
                    batch["admission_deadline"],
                    self._admission_timeout,
                )
                > 0
            ):
                continue
            initial = []
            for call in calls:
                if call["status"] in TERMINAL:
                    continue
                admission = call["admission"] or {"verdict": "deny", "error": "接纳超时"}
                call["admission"] = admission
                if state["meta"].get(f"cancelled:{call['run_id']}"):
                    self._collect(out, state, call, "cancelled", error="运行已取消，工具未执行")
                    continue
                verdict = admission["verdict"]
                if verdict == "ask":
                    call.update(
                        status="waiting_approval",
                        permission="pending",
                        approval_deadline=now + self._ttl,
                    )
                elif verdict == "allow":
                    active = sum(
                        c["status"] in {"queued", "running"} for c in state["calls"].values()
                    )
                    if active >= self._max_pending:
                        self._collect(out, state, call, "failed", error="工具执行容量已满")
                    else:
                        call.update(status="queued", deadline=now + admission["timeout"])
                        initial.append(call["invocation_id"])
                else:
                    self._collect(
                        out, state, call, "failed", error=admission.get("error", "权限拒绝")
                    )
            batch.update(admitted=True, initial_submitted=initial)

    def batch_status(self, user: User, batch_id: str) -> State | None:
        state = self.store.read(user)
        batch = state["batches"].get(batch_id)
        return (
            None
            if batch is None
            else {**batch, "calls": {i: state["calls"][i] for i in batch["members"]}}
        )

    def status(self, user: User, invocation_id: str) -> ToolExecution | None:
        call = self.store.read(user)["calls"].get(invocation_id)
        if call is None:
            return None
        result = call["terminal"] or {}
        error = result.get("error")
        return ToolExecution(
            user=user,
            invocation_id=invocation_id,
            tool_call_id=call["tool_call_id"],
            tool=call["tool"],
            arguments=call["arguments"],
            status=call["status"],
            result=ToolResult(result["content"]) if "content" in result else None,
            error=ToolErrorInfo(**error) if error else None,
            submitted_dt=call["submitted_dt"],
            started_dt=call["started_dt"],
            finished_dt=call["finished_dt"],
            batch_id=call["batch_id"],
            source=call["source"],
            event_id=result.get("event_id"),
        )

    def apply_permission(
        self, user: User, invocation_id: str, decision: str, command_id: str
    ) -> bool:
        result, produced = self.permissions.decide(user, invocation_id, decision, command_id)
        self._publish(user, list(produced))
        self.maintain(user)
        return result

    def cancel(self, user: User, run_id: str | None = None) -> None:
        out: list[Inbound] = []

        def change(state: State) -> None:
            for call in state["calls"].values():
                if run_id is not None and call["run_id"] != run_id:
                    continue
                if call["status"] not in TERMINAL:
                    running = call["status"] == "running"
                    call["source"] = "system"
                    self._collect(
                        out,
                        state,
                        call,
                        "outcome_unknown" if running else "cancelled",
                        error="已请求取消；运行中的外部副作用无法保证撤回"
                        if running
                        else "执行前取消",
                    )

        self.store.transact(user, "tool.cancelled", change)
        self._publish(user, out)
        self.maintain(user)

    def maintain(self, user: User) -> None:
        if self._stop.is_set():
            return
        out: list[Inbound] = []

        def tick(state: State) -> State:
            now = time.time()
            self._activate(user, state, now, out)
            for call in state["calls"].values():
                if (
                    call["status"] in {"queued", "running"}
                    and self._deadlines.remaining(
                        (user, call["invocation_id"], "execution"),
                        call["deadline"],
                        call["admission"]["timeout"],
                    )
                    <= 0
                ):
                    self._collect(
                        out,
                        state,
                        call,
                        "timeout",
                        error="执行 deadline 已到；外部副作用可能已发生",
                    )
                elif (
                    call["status"] == "waiting_approval"
                    and self._deadlines.remaining(
                        (user, call["invocation_id"], "approval"),
                        call["approval_deadline"],
                        self._ttl,
                    )
                    <= 0
                ):
                    out.append(
                        Inbound(
                            kind=QueueName.SYSTEM,
                            source="permission.expire",
                            source_id=f"expire:{call['invocation_id']}",
                            payload={
                                "invocation_id": call["invocation_id"],
                                "status": "expired",
                            },
                        )
                    )

            return {
                "batches": {
                    key: {"admitted": batch["admitted"]} for key, batch in state["batches"].items()
                },
                "calls": {key: {"status": call["status"]} for key, call in state["calls"].items()},
            }

        state = self.store.transact(user, "tool.deadlines", tick)
        self._publish(user, out)
        for batch_id, batch in state["batches"].items():
            if batch["admitted"]:
                self._deadlines.forget((user, batch_id, "admission"))
        for invocation_id, call in state["calls"].items():
            key = (user, invocation_id)
            if call["status"] in TERMINAL:
                self._deadlines.forget((user, invocation_id, "execution"))
                self._deadlines.forget((user, invocation_id, "approval"))
            if call["status"] == "queued":
                with self._lock:
                    if key in self._scheduled or not self._slots.acquire(blocking=False):
                        continue
                    self._scheduled.add(key)
                threading.Thread(
                    target=self._run, args=(user, invocation_id), name="oath-tool", daemon=True
                ).start()

    def _run(self, user: User, invocation_id: str) -> None:
        out: list[Inbound] = []
        observed: dict[str, Any] = {}
        started_ns = time.time_ns()
        try:

            def start(state: State) -> State | None:
                call = state["calls"][invocation_id]
                if self._stop.is_set() or call["status"] != "queued":
                    return None
                if state["meta"].get(f"cancelled:{call['run_id']}"):
                    self._collect(out, state, call, "cancelled", error="运行已取消，未执行")
                    return None
                if (
                    self._deadlines.remaining(
                        (user, invocation_id, "execution"),
                        call["deadline"],
                        call["admission"]["timeout"],
                    )
                    <= 0
                ):
                    self._collect(out, state, call, "timeout", error="排队超时，工具未执行")
                    return None
                call.update(status="running", started_dt=utc_now_iso())
                step = state["steps"].get(call["batch_id"], {})
                observed.update(
                    trace_id=step.get("trace_id", ""),
                    step_id=step.get("step_id", call["batch_id"]),
                    channel=step.get("channel", "unknown"),
                    queue_ms=_elapsed_ms(call.get("submitted_dt")),
                )
                return cast(State, call)

            call = self.store.transact(user, "tool.started", start)
            self._publish(user, out)
            out.clear()
            if call is None:
                return
            registered = self._registry.get(call["tool"])
            outcome = "failed"
            try:
                if (
                    registered is None
                    or registered.definition.to_dict() != call["admission"]["definition"]
                ):
                    raise ValueError("原工具定义已改变，未执行")
                result = registered.tool.execute(
                    ToolInvocation(
                        user,
                        invocation_id,
                        copy.deepcopy(call["arguments"]),
                        time.monotonic()
                        + self._deadlines.remaining(
                            (user, invocation_id, "execution"),
                            call["deadline"],
                            call["admission"]["timeout"],
                        ),
                        call.get("channel", "unknown"),
                    )
                )
                if self._stop.is_set():
                    return
                if not isinstance(result, ToolResult):
                    raise TypeError("工具必须返回 ToolResult")
                canonical_json(result.content)
                # 截断在事务之外完成：写 detail 是文件 I/O，不能在 user 锁里做。
                content, detail = (
                    (result.content, None)
                    if self._shrinker is None
                    else self._shrinker.shrink(call["tool"], user, invocation_id, result.content)
                )

                def finish(state: State) -> None:
                    nonlocal outcome
                    current = state["calls"][invocation_id]
                    if (
                        self._deadlines.remaining(
                            (user, invocation_id, "execution"),
                            current["deadline"],
                            current["admission"]["timeout"],
                        )
                        <= 0
                    ):
                        outcome = "timeout"
                        self._collect(
                            out, state, current, "timeout", error="执行期限已到，结果迟到"
                        )
                    else:
                        outcome = "succeeded"
                        self._collect(
                            out, state, current, "succeeded", content=content, detail=detail
                        )

                self.store.transact(user, "tool.terminal", finish)
            except Exception as exc:
                if self._stop.is_set():
                    return
                error = str(exc)
                error_kind = "tool_error" if isinstance(exc, ToolExecutionError) else "unexpected"

                outcome = "failed"

                def failed(state: State, error: str = error, kind: str = error_kind) -> None:
                    self._collect(
                        out,
                        state,
                        state["calls"][invocation_id],
                        "failed",
                        error=error,
                        error_kind=kind,
                    )

                self.store.transact(user, "tool.terminal", failed)
            finally:
                self._publish(user, out)
                self._trace_tool(
                    user,
                    invocation_id,
                    call["tool"],
                    outcome,
                    started_ns,
                    observed,
                )
                TOOL_DURATION.labels(call["tool"], outcome).observe(
                    max(0, (time.time_ns() - started_ns) / 1_000_000_000)
                )
        except Exception:
            _log.exception("工具 worker 故障；deadline / 恢复负责收尾")
        finally:
            with self._lock:
                self._scheduled.discard((user, invocation_id))
            self._slots.release()
            self.maintain(user)

    def _trace_tool(
        self,
        user: User,
        invocation_id: str,
        tool: str,
        outcome: str,
        started_ns: int,
        observed: dict[str, Any],
    ) -> None:
        trace_id = observed.get("trace_id")
        if not isinstance(trace_id, str) or len(trace_id) != 32:
            return
        step_id = str(observed.get("step_id", ""))
        try:
            self._traces.append(
                user,
                Span(
                    trace_id=trace_id,
                    span_id=hashlib.sha256(invocation_id.encode()).hexdigest()[:16],
                    parent_span_id=step_id[:16] if len(step_id) >= 16 else None,
                    name=f"execute_tool {tool}",
                    kind=SpanKind.INTERNAL,
                    start_unix_nano=started_ns,
                    end_unix_nano=time.time_ns(),
                    status=SpanStatus.OK if outcome == "succeeded" else SpanStatus.ERROR,
                    attributes={
                        OATH_STEP_ID: step_id,
                        OATH_CHANNEL: observed.get("channel", "unknown"),
                        OATH_TOOL_NAME: tool,
                        OATH_TOOL_OUTCOME: outcome,
                        OATH_TOOL_DURATION_MS: round((time.time_ns() - started_ns) / 1_000_000, 3),
                        OATH_TOOL_QUEUE_MS: observed.get("queue_ms"),
                    },
                ),
            )
        except Exception:
            _log.warning("Tool trace write failed for %s", invocation_id)

    def _watch(self) -> None:
        failures: dict[User, tuple[int, float]] = {}
        while not self._stop.wait(0.5):
            try:
                users = tuple(
                    User.from_storage_id(uid) for uid in self.store.storage.call("tools.users")
                )
            except Exception:
                _log.exception("工具状态扫描失败")
                continue
            for user in users:
                count, next_try = failures.get(user, (0, 0.0))
                if time.monotonic() < next_try:
                    continue
                try:
                    self.maintain(user)
                    failures.pop(user, None)
                except Exception:
                    count += 1
                    failures[user] = (count, time.monotonic() + min(5, 0.2 * count))
                    _log.exception("工具维护失败，将退避重试")

    def shutdown(self, *, wait: bool = True) -> None:
        self._stop.set()
        if wait:
            self._watchdog.join(timeout=2)
