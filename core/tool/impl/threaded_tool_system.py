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
from typing import cast

from jsonschema import validate

from core.buffer.journal_buffer import accept_input
from core.journal.runtime_store import RuntimeStore, State
from core.protocol.datatype.inbound import Inbound, InboundKind
from core.protocol.datatype.permission import PermissionEvent, PermissionEventType
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.tool import (
    BatchExecution,
    ToolCallRequest,
    ToolDefinition,
    ToolErrorInfo,
    ToolEvent,
    ToolEventType,
    ToolExecution,
    ToolHandle,
    ToolHandleState,
    ToolInvocation,
    ToolResult,
    is_valid_invocation_id,
)
from core.protocol.error import ToolExecutionError, ToolRequestError
from core.protocol.interface import AgentEventSink, EventQueue, PermissionPolicy, ToolSystem
from core.tool.lifecycle import TERMINAL
from core.tool.lifecycle import terminal as _terminal
from core.tool.permission import PermissionManager
from core.tool.registry import ToolRegistry
from core.utils.deadlines import Deadlines
from core.utils.encoding import canonical_json, utc_now_iso

_log = logging.getLogger(__name__)
DEFAULT_MAX_CONCURRENCY = 8
DEFAULT_MAX_PENDING = 32
MAX_ERROR_MESSAGE_LENGTH = 500


class ThreadedToolSystem(ToolSystem):
    def __init__(
        self,
        registry: ToolRegistry,
        sink: AgentEventSink,
        policy: PermissionPolicy,
        *,
        store: RuntimeStore,
        queue: EventQueue,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        max_pending: int = DEFAULT_MAX_PENDING,
        approval_ttl: float = 300,
        admission_timeout: float = 5,
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
        self.store, self._queue = store, queue
        self._registry, self._sink = registry, sink
        self._max_pending, self._ttl, self._admission_timeout = (
            max_pending,
            approval_ttl,
            admission_timeout,
        )
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._policy_slots = threading.BoundedSemaphore(max_pending)
        self._lock = threading.Lock()
        self._scopes: set[Scope] = set()
        self._scheduled: set[tuple[Scope, str]] = set()
        self._announced: set[tuple[Scope, str]] = set()
        self._last_sent: dict[tuple[Scope, str], float] = {}
        self._stop = threading.Event()
        self._deadlines = Deadlines()
        self.permissions = PermissionManager(store, policy, self._deadlines, approval_ttl)
        self._watchdog = threading.Thread(
            target=self._watch, name="oath-tool-watchdog", daemon=True
        )
        self._watchdog.start()

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._registry.definitions()

    def execute(self, scope: Scope, requests: Sequence[ToolCallRequest]) -> BatchExecution:
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
        return self.submit_batch(scope, batch_id, calls)

    def submit_batch(
        self, scope: Scope, batch_id: str, requests: Sequence[ToolCallRequest]
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

        fresh = self.store.transact(scope, "tool.batch_declared", declare)
        with self._lock:
            self._scopes.add(scope)
        if fresh:
            for item in manifest:
                invocation_id = str(item["invocation_id"])
                if self._policy_slots.acquire(blocking=False):
                    threading.Thread(
                        target=self._classify,
                        args=(scope, invocation_id),
                        name="oath-tool-admission",
                        daemon=True,
                    ).start()
                else:
                    self._classified(
                        scope, invocation_id, {"verdict": "deny", "error": "接纳容量已满"}
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

    def _classify(self, scope: Scope, invocation_id: str) -> None:
        try:
            call = self.store.read(scope)["calls"][invocation_id]
            registered = self._registry.get(call["tool"])
            if registered is None:
                raise ValueError("工具未注册")
            verdict = self.permissions.authorize(
                scope, call["tool"], copy.deepcopy(call["arguments"])
            )
            if verdict not in {"allow", "ask", "deny"}:
                raise ValueError("未知权限判定")
            if verdict != "deny":
                validate(call["arguments"], registered.definition.input_schema)
            admission = {
                "verdict": verdict,
                "timeout": registered.timeout_seconds,
                "definition": registered.definition.to_dict(),
            }
            self._classified(scope, invocation_id, admission)
        except Exception as exc:
            self._classified(
                scope,
                invocation_id,
                {"verdict": "deny", "error": str(exc)[:MAX_ERROR_MESSAGE_LENGTH]},
            )
        finally:
            self._policy_slots.release()

    def _classified(self, scope: Scope, invocation_id: str, admission: State) -> None:
        if self._stop.is_set():
            return

        def change(state: State) -> None:
            call = state["calls"][invocation_id]
            if call["status"] == "admitting" and call["admission"] is None:
                call["admission"] = admission

        self.store.transact(scope, "tool.classified", change)
        self.maintain(scope)

    def _activate(self, scope: Scope, state: State, now: float) -> None:
        for batch in state["batches"].values():
            if batch["admitted"]:
                continue
            calls = [state["calls"][i] for i in batch["members"]]
            if (
                any(c["admission"] is None and c["status"] not in TERMINAL for c in calls)
                and self._deadlines.remaining(
                    (scope, calls[0]["batch_id"], "admission"),
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
                    _terminal(state, call, "cancelled", error="运行已取消，工具未执行")
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
                        _terminal(state, call, "failed", error="工具执行容量已满")
                    else:
                        call.update(status="queued", deadline=now + admission["timeout"])
                        initial.append(call["invocation_id"])
                else:
                    _terminal(state, call, "failed", error=admission.get("error", "权限拒绝"))
            batch.update(admitted=True, initial_submitted=initial)

    def batch_status(self, scope: Scope, batch_id: str) -> State | None:
        state = self.store.read(scope)
        batch = state["batches"].get(batch_id)
        return (
            None
            if batch is None
            else {**batch, "calls": {i: state["calls"][i] for i in batch["members"]}}
        )

    def status(self, scope: Scope, invocation_id: str) -> ToolExecution | None:
        call = self.store.read(scope)["calls"].get(invocation_id)
        if call is None:
            return None
        result = call["terminal"] or {}
        error = result.get("error")
        return ToolExecution(
            scope=scope,
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
        self, scope: Scope, invocation_id: str, decision: str, command_id: str
    ) -> bool:
        result = self.permissions.decide(scope, invocation_id, decision, command_id)
        self.maintain(scope)
        return result

    def cancel(self, scope: Scope, run_id: str | None = None) -> None:
        def change(state: State) -> None:
            for call in state["calls"].values():
                if run_id is not None and call["run_id"] != run_id:
                    continue
                if call["status"] not in TERMINAL:
                    running = call["status"] == "running"
                    call["source"] = "system"
                    _terminal(
                        state,
                        call,
                        "outcome_unknown" if running else "cancelled",
                        error="已请求取消；运行中的外部副作用无法保证撤回"
                        if running
                        else "执行前取消",
                    )

        self.store.transact(scope, "tool.cancelled", change)
        self.maintain(scope)

    def recover_scope(self, scope: Scope) -> None:
        def change(state: State) -> None:
            for call in state["calls"].values():
                if call["status"] == "running":
                    _terminal(
                        state,
                        call,
                        "outcome_unknown",
                        error="进程中断，无法确认外部执行结果；未自动重跑",
                    )

        self.store.transact(scope, "tool.recovered", change)
        with self._lock:
            self._scopes.add(scope)
        self.maintain(scope)

    def maintain(self, scope: Scope) -> None:
        if self._stop.is_set():
            return

        def tick(state: State) -> None:
            now = time.time()
            self._activate(scope, state, now)
            for call in state["calls"].values():
                if (
                    call["status"] in {"queued", "running"}
                    and self._deadlines.remaining(
                        (scope, call["invocation_id"], "execution"),
                        call["deadline"],
                        call["admission"]["timeout"],
                    )
                    <= 0
                ):
                    _terminal(
                        state, call, "timeout", error="执行 deadline 已到；外部副作用可能已发生"
                    )
                elif (
                    call["status"] == "waiting_approval"
                    and self._deadlines.remaining(
                        (scope, call["invocation_id"], "approval"),
                        call["approval_deadline"],
                        self._ttl,
                    )
                    <= 0
                ):
                    accept_input(
                        state,
                        Inbound(
                            kind=InboundKind.INVOKE,
                            source="permission.expire",
                            source_id=f"expire:{call['invocation_id']}",
                            payload={"invocation_id": call["invocation_id"], "status": "expired"},
                        ),
                    )

        self.store.transact(scope, "tool.deadlines", tick)
        state = self.store.read(scope)
        for invocation_id, call in state["calls"].items():
            key = (scope, invocation_id)
            if call["status"] == "queued":
                with self._lock:
                    if key in self._scheduled or not self._slots.acquire(blocking=False):
                        continue
                    self._scheduled.add(key)
                threading.Thread(
                    target=self._run, args=(scope, invocation_id), name="oath-tool", daemon=True
                ).start()
            elif call["status"] == "waiting_approval":
                with self._lock:
                    announce = key not in self._announced
                    if announce:
                        self._announced.add(key)
                if announce:
                    try:
                        self._sink.emit(
                            PermissionEvent(
                                type=PermissionEventType.REQUESTED,
                                scope=scope,
                                status="pending",
                                tool=call["tool"],
                                invocation_id=invocation_id,
                                tool_call_id=call["tool_call_id"],
                                payload={
                                    "invocation_id": invocation_id,
                                    "batch_id": call["batch_id"],
                                    "tool": call["tool"],
                                    "arguments": call["arguments"],
                                    "expires_at": call["approval_deadline"],
                                },
                                idempotency_key=f"permission:{invocation_id}",
                                occur_dt=utc_now_iso(),
                            )
                        )
                    except Exception:
                        with self._lock:
                            self._announced.discard(key)
                        _log.exception("审批通知失败，状态可查询")
        for key, entry in state["inputs"].items():
            if entry["phase"] != "accepted" or entry["queue"] == "user":
                continue
            with self._lock:
                now = time.monotonic()
                if now - self._last_sent.get((scope, key), -1) < 0.2:
                    continue
                self._last_sent[(scope, key)] = now
            item = Inbound.from_dict(entry["item"])
            self._queue.put(scope, item)
            if item.kind == InboundKind.TOOL_RESULT:
                try:
                    p = item.payload
                    self._sink.emit(
                        ToolEvent(
                            type=ToolEventType.TERMINAL,
                            scope=scope,
                            outcome=str(p["outcome"]),
                            invocation_id=str(p["invocation_id"]),
                            tool_call_id=str(p["tool_call_id"]),
                            payload=p,
                            idempotency_key=item.source_id,
                            occur_dt=item.occur_dt,
                        )
                    )
                except Exception:
                    _log.exception("终态通知失败；已保存结果保持不变")

    def _run(self, scope: Scope, invocation_id: str) -> None:
        try:

            def start(state: State) -> State | None:
                call = state["calls"][invocation_id]
                if self._stop.is_set() or call["status"] != "queued":
                    return None
                if state["meta"].get(f"cancelled:{call['run_id']}"):
                    _terminal(state, call, "cancelled", error="运行已取消，未执行")
                    return None
                if (
                    self._deadlines.remaining(
                        (scope, invocation_id, "execution"),
                        call["deadline"],
                        call["admission"]["timeout"],
                    )
                    <= 0
                ):
                    _terminal(state, call, "timeout", error="排队超时，工具未执行")
                    return None
                call.update(status="running", started_dt=utc_now_iso())
                return cast(State, call)

            call = self.store.transact(scope, "tool.started", start)
            if call is None:
                return
            registered = self._registry.get(call["tool"])
            try:
                if (
                    registered is None
                    or registered.definition.to_dict() != call["admission"]["definition"]
                ):
                    raise ValueError("原工具定义已改变，未执行")
                result = registered.tool.execute(
                    ToolInvocation(
                        scope,
                        invocation_id,
                        copy.deepcopy(call["arguments"]),
                        time.monotonic()
                        + self._deadlines.remaining(
                            (scope, invocation_id, "execution"),
                            call["deadline"],
                            call["admission"]["timeout"],
                        ),
                    )
                )
                if self._stop.is_set():
                    return
                if not isinstance(result, ToolResult):
                    raise TypeError("工具必须返回 ToolResult")
                canonical_json(result.content)

                def finish(state: State) -> None:
                    current = state["calls"][invocation_id]
                    if (
                        self._deadlines.remaining(
                            (scope, invocation_id, "execution"),
                            current["deadline"],
                            current["admission"]["timeout"],
                        )
                        <= 0
                    ):
                        _terminal(state, current, "timeout", error="执行期限已到，结果迟到")
                    else:
                        _terminal(state, current, "succeeded", content=result.content)

                self.store.transact(scope, "tool.terminal", finish)
            except Exception as exc:
                if self._stop.is_set():
                    return
                error = str(exc)
                error_kind = "tool_error" if isinstance(exc, ToolExecutionError) else "unexpected"
                self.store.transact(
                    scope,
                    "tool.terminal",
                    lambda s: _terminal(
                        s, s["calls"][invocation_id], "failed", error=error, error_kind=error_kind
                    ),
                )
        except Exception:
            _log.exception("工具 worker 故障；deadline / 恢复负责收尾")
        finally:
            with self._lock:
                self._scheduled.discard((scope, invocation_id))
            self._slots.release()
            self.maintain(scope)

    def _watch(self) -> None:
        failures: dict[Scope, tuple[int, float]] = {}
        while not self._stop.wait(0.05):
            with self._lock:
                scopes = tuple(self._scopes)
            for scope in scopes:
                count, next_try = failures.get(scope, (0, 0.0))
                if time.monotonic() < next_try:
                    continue
                try:
                    self.maintain(scope)
                    failures.pop(scope, None)
                except Exception:
                    count += 1
                    failures[scope] = (count, time.monotonic() + min(5, 0.2 * count))
                    _log.exception("工具维护失败，将退避重试")

    def shutdown(self, *, wait: bool = True) -> None:
        self._stop.set()
        if wait:
            self._watchdog.join(timeout=2)
