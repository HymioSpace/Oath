"""按 scope 协调唤醒：从 buffer 认领输入、原子落盘、交给 Brain。"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol, cast

from core.consumer import input_key
from core.context.intake import build_messages
from core.journal.runtime_store import State
from core.protocol.datatype import Scope
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import QueueName
from core.protocol.datatype.trace import SpanLink, new_trace_id
from core.runtime import AgentContext
from core.trace.links import source_links

_log = logging.getLogger(__name__)


class StepExecutor(Protocol):
    def system_prompt(self) -> str: ...
    def execute(self, scope: Scope, step: State) -> None: ...
    def open_step(
        self,
        scope: Scope,
        step: State,
        claimed: list[tuple[str, float | None, State]],
        links: tuple[SpanLink, ...],
    ) -> None: ...
    def emit(self, scope: Scope, type_: str, payload: State) -> None: ...


@dataclass
class _Worker:
    signal: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    failures: int = 0
    faulted: bool = False
    retry_seq: int = 0


class IntakeCoordinator:
    def __init__(self, ctx: AgentContext, executor: StepExecutor, stop: threading.Event) -> None:
        self._ctx, self._store, self.executor = ctx, ctx.store, executor
        self._stop = stop
        self._lock = threading.Lock()
        self._workers: dict[Scope, _Worker] = {}

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

    def wake(self, scope: Scope) -> None:
        if self._stop.is_set():
            return
        self._worker(scope).signal.set()

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

    def _peek(self, scope: Scope) -> list[tuple[str, Inbound]]:
        """读出 buffer 里现在有的全部输入，**不移出**。

        认领是两步：读出来、写进 journal、然后才从 buffer 删掉那几条。先删后写的话，
        崩在中间两头都没有 —— 输入没了，journal 里也没有。
        """
        found: list[tuple[str, Inbound]] = []
        for queue in (QueueName.USER, QueueName.TOOL, QueueName.SYSTEM):
            found.extend((queue, item) for item in self._ctx.buffer.peek(scope, queue))
        return found

    def _discard_claimed(self, scope: Scope, items: list[tuple[str, Inbound]]) -> None:
        """从 buffer 里删掉已经进了 journal 的那几条。

        按身份删，不是按位置 —— journal 是唯一事实来源，它说这条认过了就是认过了。
        崩在提交之后、删除之前的那几条会在下一轮认领时撞上 `input_key` 已经在
        `state["inputs"]` 里，于是这一句把它们收走，不会卡在那儿反复重试。
        """
        claimed = self._store.read(scope)["inputs"]
        for queue, item in items:
            if input_key(item) in claimed:
                self._ctx.buffer.remove(scope, queue, item)

    def claim(self, scope: Scope) -> State | None:
        """认领一次输入：读出 buffer 里的全部，落进 journal，开一个 step。

        **落盘发生在这一步，不是到达的时候。** 认领之前输入住在 buffer 里（持久化）；
        认领之后它才成为 journal 里的事实。

        三步的顺序是刻意排的：读 → 写 journal → 从 buffer 删。每一步崩了都还在：
        崩在写之前输入还在 buffer 里等下一轮，崩在删之前 journal 里有、buffer 里有同一件
        事，下一轮认领时按身份去重并把 buffer 里那份收掉。
        """
        if self._store.read(scope)["meta"].get("intake_error"):
            return None
        state = self._store.read(scope)
        active = state["meta"].get("active_step")
        if active:
            # 有在飞的 step 先把它跑完；buffer 里的输入等下一轮。落在终态上的不返回 ——
            # 返回它等于让这一轮再跑一遍已经结束的事。
            step = cast(State, state["steps"][active])
            return step if step["status"] == "prepared" else None
        items = self._peek(scope)
        if not items:
            return None
        definitions = [t.to_dict() for t in self._ctx.tools.definitions()]
        config = self._ctx.config
        fresh = False
        claimed: list[tuple[str, float | None, State]] = []
        links: list[SpanLink] = []

        def claim(state: State) -> State | None:
            nonlocal fresh
            if state["meta"].get("intake_error"):
                return None
            now = time.time()
            entries: list[tuple[str, State]] = []
            for queue, item in items:
                key = input_key(item)
                if key in state["inputs"]:
                    # 同一件事认领过两次。journal 是唯一事实来源，后来的那次不算新的。
                    continue
                state["inputs"][key] = {
                    "item": item.to_dict(),
                    "queue": queue,
                    "phase": "claimed",
                    "buffered_at": now,
                    "step_id": None,
                }
                entries.append((key, state["inputs"][key]))
            if not entries:
                return None
            step_id = uuid.uuid4().hex
            new_user = any(entry["queue"] == QueueName.USER for _, entry in entries)
            new_run = new_user or not state["meta"].get("run_id")
            if new_run:
                state["meta"].update(run_id=uuid.uuid4().hex, run_steps=0, run_started=now)
            # 续不续上一条 trace 只看两件事：是不是新 run，上一个 step 有没有产出工具
            # 调用。新 run 必开新 trace —— 工具还在跑时插进来的那句话属于新意图。
            continuing = not new_run and state["meta"].get("trace_step")
            trace_id = state["meta"]["trace_id"] if continuing else new_trace_id()
            messages = build_messages(state, entries, step_id)
            system = self.executor.system_prompt()
            request_messages = (
                [{"role": "system", "content": system}] if system else []
            ) + messages
            exhausted = (
                state["meta"].get("run_steps", 0) >= config.max_run_steps
                or now - state["meta"]["run_started"] >= config.max_run_seconds
            )
            step = {
                "step_id": step_id,
                "run_id": state["meta"]["run_id"],
                "trace_id": trace_id,
                "trace_started": time.time_ns(),
                "input_ids": [key for key, _ in entries],
                "trigger": sorted({entry["queue"] for _, entry in entries}),
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
            state["meta"]["trace_id"] = trace_id
            fresh = True
            claimed.extend(
                (entry["queue"], entry["buffered_at"], entry["item"]) for _, entry in entries
            )
            links.extend(source_links(state, [entry for _, entry in entries]))
            state["conversation"]["messages"] = messages
            for _, entry in entries:
                entry["step_id"] = step_id
            return step

        try:
            # 名字与上面那个 `step` 分开：同一个名字两次赋值，mypy 会拿前一次的类型当
            # `transact` 的返回值约束，于是这里推成永不返回 None。
            opened: State | None = self._store.transact(scope, "step.input", claim)
        except Exception:
            # 输入还在 buffer 里，没删过任何东西 —— 不用放回去，让它重试。
            raise
        # 事务提交了就把它从 buffer 收走。提交失败时 journal 里没有，这里查到的就只有
        # 之前就认过的那些 —— 认过的收走，没认过的留在 buffer 里等下一轮。
        self._discard_claimed(scope, items)
        if opened is None:
            return None
        if fresh:
            # 事务外写 span：文件 I/O 不能在 scope 锁里做。
            self.executor.open_step(scope, opened, claimed, tuple(links))
            if opened["status"] == "paused":
                self.executor.emit(
                    scope,
                    "step.status",
                    {"step_id": opened["step_id"], "status": "paused", "error": opened["error"]},
                )
        return opened

    def _run(self, scope: Scope, worker: _Worker) -> None:
        while not self._stop.is_set():
            worker.signal.wait()
            if self._stop.is_set():
                return
            worker.signal.clear()
            if worker.faulted:
                continue
            try:
                step = self.claim(scope)
                if step is None or step["status"] in {"failed", "paused"}:
                    continue
                self.executor.execute(scope, step)
                worker.failures = 0
                # 走完再看一眼 buffer。执行期间到达的输入，它的唤醒已经被这一轮用掉了 ——
                # 不在这里补一次，它要一直等到下一次唤醒才有人认领。
                worker.signal.set()
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
                    self.executor.emit(scope, "step.status", {"status": "failed", "error": error})
                    continue
                self._stop.wait(self._ctx.config.llm_retry_delay * worker.failures)
                worker.signal.set()

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


__all__ = ["IntakeCoordinator"]
