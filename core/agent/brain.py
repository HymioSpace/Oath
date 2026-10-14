"""每个 session 串行执行；消费者和工具 worker 不被模型请求阻塞。"""

from __future__ import annotations

import copy
import itertools
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

from core.buffer.journal_buffer import buffered
from core.compaction.coordinator import CompactionCoordinator
from core.compaction.tokens import estimate_text
from core.context.intake import build_messages
from core.journal.runtime_store import State
from core.protocol.datatype import Message, Scope, ToolCallRequest, ToolDefinition
from core.protocol.datatype.llm import LlmRequest, LlmResponse
from core.protocol.datatype.message import MessageEvent, MessageEventType
from core.protocol.interface.brain import Brain
from core.protocol.interface.llm import ModelProvider
from core.runtime import AgentContext
from core.utils.encoding import canonical_json, utc_now_iso

_log = logging.getLogger(__name__)


@dataclass
class _Worker:
    signal: threading.Event = field(default_factory=threading.Event)
    triggers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    thread: threading.Thread | None = None
    failures: int = 0
    faulted: bool = False
    retry_seq: int = 0


class AgentBrain(Brain):
    def __init__(
        self, ctx: AgentContext, provider: ModelProvider, compaction: CompactionCoordinator
    ) -> None:
        self._ctx, self._provider = ctx, provider
        self._store = ctx.store
        self._compaction = compaction
        self._lock = threading.Lock()
        self._workers: dict[Scope, _Worker] = {}
        self._stop = threading.Event()

    def _worker(self, scope: Scope) -> _Worker:
        with self._lock:
            worker = self._workers.get(scope)
            if worker is None:
                worker = _Worker()
                self._workers[scope] = worker
                worker.thread = threading.Thread(
                    target=self._run,
                    args=(scope, worker),
                    name=f"oath-brain-{scope.session_id}",
                    daemon=True,
                )
                worker.thread.start()
            return worker

    def wake(self, scope: Scope, trigger: str, event_ids: tuple[str, ...]) -> None:
        if self._stop.is_set() or not event_ids:
            return
        worker = self._worker(scope)
        with self._lock:
            worker.triggers[trigger] = event_ids
        worker.signal.set()

    def resume(self, scope: Scope) -> None:
        if self._stop.is_set():
            return
        worker = self._worker(scope)
        retry_seq = self._store.read(scope)["meta"].get("retry_seq", 0)
        with self._lock:
            if retry_seq != worker.retry_seq:
                worker.retry_seq = retry_seq
                worker.failures = 0
                worker.faulted = False
        worker.signal.set()

    def retry(self, scope: Scope) -> None:
        def change(state: State) -> None:
            state["meta"]["retry_seq"] = state["meta"].get("retry_seq", 0) + 1
            state["meta"]["intake_error"] = None
            state["meta"].update(run_steps=0, run_started=time.time())
            step_id = state["meta"].get("active_step")
            if step_id:
                step = state["steps"][step_id]
                if step["status"] in {"failed", "paused"}:
                    step.update(status="prepared", attempts=0, error=None)

        self._store.transact(scope, "step.retry", change)
        self.resume(scope)

    def _claim(self, scope: Scope, triggers: dict[str, tuple[str, ...]]) -> State | None:
        definitions = [t.to_dict() for t in self._ctx.tools.definitions()]
        config = self._ctx.config
        fresh = False

        def claim(state: State) -> State | None:
            nonlocal fresh
            if state["meta"].get("intake_error"):
                return None
            active = state["meta"].get("active_step")
            if active:
                return cast(State, state["steps"][active])
            entries = buffered(state)
            current_ids = {key for key, _ in entries}
            if not any(current_ids.intersection(ids) for ids in triggers.values()):
                return None
            step_id = uuid.uuid4().hex
            new_user = any(e["queue"] == "user" for _, e in entries)
            if new_user or not state["meta"].get("run_id"):
                state["meta"].update(run_id=uuid.uuid4().hex, run_steps=0, run_started=time.time())
            messages = build_messages(state, entries, step_id)
            request_messages = (
                [{"role": "system", "content": config.system_prompt}]
                if config.system_prompt
                else []
            ) + messages
            exhausted = (
                state["meta"].get("run_steps", 0) >= config.max_run_steps
                or time.time() - state["meta"]["run_started"] >= config.max_run_seconds
            )
            step = {
                "step_id": step_id,
                "run_id": state["meta"]["run_id"],
                "input_ids": [key for key, _ in entries],
                "trigger": list(triggers),
                "status": "paused" if exhausted else "prepared",
                "attempts": 0,
                "error": "run 预算已用尽，请显式继续" if exhausted else None,
                "response": None,
                "request": None,
                "request_draft": {
                    "messages": request_messages,
                    "tools": definitions,
                    "model": config.provider.model,
                    "max_output_tokens": config.max_output_tokens,
                    "temperature": config.temperature,
                    "extra_params": config.provider.extra_params,
                },
            }
            state["steps"][step_id] = step
            state["meta"]["active_step"] = step_id
            fresh = True
            state["conversation"]["messages"] = messages
            for _, entry in entries:
                entry.update(phase="claimed", step_id=step_id)
            return step

        step = self._store.transact(scope, "step.input", claim)
        if fresh:
            self._ctx.queue.notify(scope)
            if step and step["status"] == "paused":
                self._emit(
                    scope,
                    "step.status",
                    {"step_id": step["step_id"], "status": "paused", "error": step["error"]},
                )
        return step

    def _run(self, scope: Scope, worker: _Worker) -> None:
        while not self._stop.is_set():
            worker.signal.wait()
            if self._stop.is_set():
                return
            worker.signal.clear()
            if worker.faulted:
                continue
            with self._lock:
                triggers = worker.triggers
                worker.triggers = {}
            try:
                step = self._claim(scope, triggers)
                if step is None or step["status"] in {"failed", "paused"}:
                    continue
                self._execute(scope, step)
                worker.failures = 0
            except Exception as exc:
                _log.exception("Brain 调度失败，保留原输入")
                worker.failures += 1
                if worker.failures >= self._ctx.config.llm_max_attempts:
                    worker.faulted = True
                    error = str(exc)[:500]

                    def record(state: State, error: str = error) -> None:
                        active = state["meta"].get("active_step")
                        if active:
                            state["steps"][active].update(status="failed", error=error)
                        else:
                            state["meta"]["intake_error"] = error

                    try:
                        self._store.transact(scope, "step.scheduler_failed", record)
                    except Exception:
                        _log.exception("无法保存故障状态；推理已在进程内暂停")
                    self._emit(scope, "step.status", {"status": "failed", "error": error})
                    continue
                self._stop.wait(self._ctx.config.llm_retry_delay * worker.failures)
                with self._lock:
                    worker.triggers.update(triggers)
                worker.signal.set()

    def _prepare_request(self, scope: Scope, step: State) -> State:
        if step["request"] is not None:
            return step
        draft = step["request_draft"]
        messages = draft["messages"]
        # system prompt 已在 draft；Coordinator 负责加入固定开销，不能重复计入。
        overhead = estimate_text(canonical_json(draft["tools"])) + draft["max_output_tokens"]
        prompt = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        self._compaction.prepare(scope, overhead=overhead, system_prompt=prompt)
        if self._stop.is_set():
            return step

        def freeze(state: State) -> State:
            current = state["steps"][step["step_id"]]
            if current["request"] is None:
                prefix = messages[:1] if messages and messages[0]["role"] == "system" else []
                history = messages[len(prefix) :]
                request = {**draft, "messages": prefix + self._compaction.view(history, state)}
                current["request"] = request
                current["request_draft"] = None
            return cast(State, current)

        return self._store.transact(scope, "step.request", freeze)

    def _execute(self, scope: Scope, step: State) -> None:
        if step["response"] is None:
            step = self._prepare_request(scope, step)
        if self._stop.is_set():
            return
        step_id = step["step_id"]
        if step["response"] is None:
            if step["attempts"] >= self._ctx.config.llm_max_attempts:
                self._fail(scope, step_id, "模型重试次数已耗尽")
                return

            def attempt(state: State) -> None:
                state["steps"][step_id]["attempts"] += 1

            self._store.transact(scope, "step.attempt", attempt)
            try:
                response = self._provider.complete(
                    _request(step["request"]), on_delta=self._delta(scope, step_id)
                )
                if self._stop.is_set():
                    return
                payload = _response(response)
            except Exception as exc:
                self._fail(scope, step_id, str(exc))
                return

            def save(state: State) -> None:
                state["steps"][step_id]["response"] = payload

            self._store.transact(scope, "step.response", save)
            step["response"] = payload
            self._emit(scope, "assistant.message", {**payload, "step_id": step_id})
        # 已保存响应后只补后处理；不重新调用模型。
        cancelled = self._store.read(scope)["meta"].get(f"cancelled:{step['run_id']}")
        calls = [] if cancelled else step["response"]["tool_calls"]
        if calls:
            self._ctx.tools.submit_batch(
                scope,
                step_id,
                [
                    ToolCallRequest(
                        tool=c["name"],
                        arguments=c["arguments"],
                        tool_call_id=c["id"],
                        invocation_id=f"{step_id}-{i}",
                        batch_id=step_id,
                        run_id=step["run_id"],
                    )
                    for i, c in enumerate(calls)
                ],
            )

        def finish(state: State) -> None:
            current = state["steps"][step_id]
            if current["status"] == "completed":
                return
            message: State = {
                "role": "assistant",
                "content": "运行已取消。" if cancelled else current["response"]["content"],
            }
            if calls:
                message["tool_calls"] = calls
            if current["response"].get("reasoning") is not None:
                message["reasoning"] = current["response"]["reasoning"]
            state["conversation"]["messages"].append(message)
            for key in current["input_ids"]:
                state["inputs"][key]["phase"] = "done"
            current["status"] = "completed"
            state["meta"]["active_step"] = None
            state["meta"]["run_steps"] = state["meta"].get("run_steps", 0) + 1

        self._store.transact(scope, "step.completed", finish)
        self._ctx.queue.notify(scope)
        # Consumer 会重查各自规则；不能仅因 buffer 非空再次调用模型。

    def _fail(self, scope: Scope, step_id: str, error: str) -> None:
        def change(state: State) -> str:
            step = state["steps"][step_id]
            failed = step["attempts"] >= self._ctx.config.llm_max_attempts
            step.update(status="failed" if failed else "prepared", error=error[:500])
            return str(step["status"])

        status = self._store.transact(scope, "step.error", change)
        self._emit(
            scope, "step.status", {"step_id": step_id, "status": status, "error": error[:500]}
        )
        if status == "prepared":
            self._stop.wait(self._ctx.config.llm_retry_delay)
            self.resume(scope)

    def _delta(self, scope: Scope, step_id: str) -> Any:
        counter = itertools.count(1)

        def emit(channel: str, text: str) -> None:
            self._emit(
                scope,
                MessageEventType.DELTA,
                {"step_id": step_id, "seq": next(counter), "channel": channel, "text": text},
            )

        return emit

    def _emit(self, scope: Scope, type_: str, payload: State) -> None:
        try:
            self._ctx.sink.emit(
                MessageEvent(
                    type=type_,
                    scope=scope,
                    payload=payload,
                    idempotency_key="",
                    occur_dt=utc_now_iso(),
                )
            )
        except Exception:
            _log.exception("Brain 通知失败，已保存状态可查询")

    def close(self, *, wait: bool = True) -> None:
        self._stop.set()
        with self._lock:
            workers = tuple(self._workers.values())
        for worker in workers:
            worker.signal.set()
        if wait:
            for worker in workers:
                if worker.thread:
                    worker.thread.join(timeout=2)


def _request(raw: State) -> LlmRequest:
    messages = [
        Message(
            m["role"], m["content"], {k: v for k, v in m.items() if k not in {"role", "content"}}
        )
        for m in raw["messages"]
    ]
    return LlmRequest(
        messages=messages,
        tools=tuple(ToolDefinition(**t) for t in raw["tools"]),
        model=raw["model"],
        max_output_tokens=raw["max_output_tokens"],
        temperature=raw["temperature"],
        extra_params=copy.deepcopy(raw["extra_params"]),
    )


def _response(response: LlmResponse) -> State:
    ids = [c.tool_call_id for c in response.tool_calls]
    if (
        len(ids) != len(set(ids))
        or any(not isinstance(i, str) or not i for i in ids)
        or any(not isinstance(c.arguments, dict) for c in response.tool_calls)
    ):
        raise ValueError("模型返回缺失或重复的 tool_call_id，整批未执行")
    return {
        "content": response.content,
        "reasoning": response.reasoning,
        "usage": response.usage,
        "tool_calls": [
            {"id": c.tool_call_id, "name": c.tool, "arguments": c.arguments}
            for c in response.tool_calls
        ],
    }
