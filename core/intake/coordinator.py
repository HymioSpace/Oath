"""按 user 协调唤醒：从 buffer 认领输入、原子落盘、交给 Brain。"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Protocol, cast

from core.consumer import input_key
from core.context.intake import build_messages
from core.journal.runtime_store import State
from core.protocol.datatype import User
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import QueueName
from core.protocol.datatype.trace import SpanLink, new_trace_id
from core.scheduling.tool_budget import exhausted, hold, reset
from core.trace.links import source_links

_log = logging.getLogger(__name__)


class StepExecutor(Protocol):
    def system_prompt(self) -> str: ...
    def execute(self, user: User, step: State) -> None: ...
    def open_step(
        self,
        user: User,
        step: State,
        claimed: list[tuple[str, float | None, State]],
        links: tuple[SpanLink, ...],
    ) -> None: ...
    def emit(self, user: User, type_: str, payload: State) -> None: ...


class IntakeCoordinator:
    """只认领固定 StepInput；执行权由 Manager 的持久租约提供。"""

    def __init__(self, ctx, executor, stop):
        self._ctx, self._store, self.executor = ctx, ctx.store, executor
        self._stop = stop

    def _peek(self, user: User) -> list[tuple[str, Inbound]]:
        """Read pending inputs; the state commit removes only the claimed identities."""
        found: list[tuple[str, Inbound]] = []
        for queue in (QueueName.USER, QueueName.TOOL, QueueName.SYSTEM):
            found.extend((queue, item) for item in self._ctx.buffer.peek(user, queue))
        return found

    def claim(self, user: User) -> State | None:
        """Freeze StepInput. Journal, state and inbox removal commit atomically."""
        if self._store.read(user)["meta"].get("intake_error"):
            return None
        state = self._store.read(user)
        active = state["meta"].get("active_step")
        if active:
            # 有在飞的 step 先把它跑完；buffer 里的输入等下一轮。落在终态上的不返回 ——
            # 返回它等于让这一轮再跑一遍已经结束的事。
            step = cast(State, state["steps"][active])
            return step if step["status"] == "prepared" else None
        items = self._peek(user)
        if not items:
            return None
        definitions = [t.to_dict() for t in self._ctx.tools.definitions()]
        config = self._ctx.config
        fresh = False
        claimed: list[tuple[str, float | None, State]] = []
        links: list[SpanLink] = []

        def claim(state: State) -> State | None:
            nonlocal fresh
            fresh = False
            claimed.clear()
            links.clear()
            if state["meta"].get("intake_error"):
                return None
            now = time.time()
            entries: list[tuple[str, State]] = []
            for queue, item in items:
                key = input_key(item)
                if key in state["inputs"]:
                    # 同一件事认领过两次。journal 是唯一事实来源，后来的那次不算新的。
                    continue
                entry = {
                    "item": item.to_dict(),
                    "queue": queue,
                    "phase": "claimed",
                    "buffered_at": now,
                    "step_id": None,
                }
                entries.append((key, entry))
            if not entries:
                return None
            external = [key for key, entry in entries if entry["queue"] != QueueName.TOOL]
            if external:
                for key in external:
                    reset(state, key)
            elif exhausted(state, config.max_tool_only_steps):
                hold(state, config.max_tool_only_steps)
                return None
            for key, entry in entries:
                state["inputs"][key] = entry
            step_id = uuid.uuid4().hex
            new_user = any(entry["queue"] == QueueName.USER for _, entry in entries)
            new_run = new_user or not state["meta"].get("run_id")
            if new_run:
                state["meta"].update(run_id=uuid.uuid4().hex, run_steps=0, run_started=now)
            # 续不续上一条 trace 只看两件事：是不是新 run，上一个 step 有没有产出工具
            # 调用。新 run 必开新 trace —— 工具还在跑时插进来的那句话属于新意图。
            continuing = not new_run and state["meta"].get("trace_step")
            trace_id = state["meta"]["trace_id"] if continuing else new_trace_id()
            selected_model = state["meta"].get("selected_model", config.provider.model)
            allowed = {config.provider.model, *config.selectable_models}
            for _, entry in entries:
                if entry["queue"] == QueueName.USER:
                    candidate = entry["item"]["payload"].get("model")
                    if isinstance(candidate, str) and candidate in allowed:
                        selected_model = candidate
            if selected_model not in allowed:
                selected_model = config.provider.model
            state["meta"]["selected_model"] = selected_model
            messages = build_messages(state, entries, step_id)
            system = self.executor.system_prompt()
            request_messages = (
                [{"role": "system", "content": system}] if system else []
            ) + messages
            run_exhausted = (
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
                "status": "paused" if run_exhausted else "prepared",
                "attempts": 0,
                "error": "run 预算已用尽，请显式继续" if run_exhausted else None,
                "response": None,
                "request": None,
                "request_draft": {
                    "messages": request_messages,
                    "tools": definitions,
                    "model": selected_model,
                    "max_output_tokens": config.max_output_tokens,
                    "temperature": config.temperature,
                    "extra_params": config.provider.extra_params,
                },
            }
            state["steps"][step_id] = step
            if not external and not run_exhausted:
                state["meta"]["tool_only_steps"] = state["meta"].get("tool_only_steps", 0) + 1
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

        opened: State | None = self._store.transact(user, "step.input", claim)
        if opened is None:
            return None
        if fresh:
            # 事务外写 span：文件 I/O 不能在 user 锁里做。
            self.executor.open_step(user, opened, claimed, tuple(links))
            if opened["status"] == "paused":
                self.executor.emit(
                    user,
                    "step.status",
                    {"step_id": opened["step_id"], "status": "paused", "error": opened["error"]},
                )
        return opened


__all__ = ["IntakeCoordinator"]
