"""三条队列各自消费、buffer 与判断唤醒。Brain 不参与 ready 判断。"""

from __future__ import annotations

import logging
import threading
import time

from core.buffer.journal_buffer import JournalEventBuffer, buffered, input_id
from core.journal.runtime_store import State
from core.protocol.datatype import Scope
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.message import MessageEvent
from core.protocol.datatype.queue import QueueName
from core.protocol.interface.brain import Brain
from core.runtime import AgentContext
from core.utils.deadlines import Deadlines
from core.utils.encoding import utc_now_iso

_log = logging.getLogger(__name__)
# 同时负责观察 presented / owner 变化。独立订阅自身 queue，不被其他队列的非空状态忙唤醒。
_RECHECK_SECONDS = 5.0


class QueueConsumer:
    queue_name: str

    def __init__(self, ctx: AgentContext, brain: Brain, buffers: JournalEventBuffer) -> None:
        self.ctx, self.brain, self.buffers = ctx, brain, buffers
        self._deadlines = Deadlines()

    def run(self, scope: Scope, stop: threading.Event) -> None:
        while not stop.is_set():
            revision = self.ctx.queue.revision(scope, self.queue_name)
            delay = _RECHECK_SECONDS
            try:
                self.consume(scope)
                delay = self.evaluate(scope)
            except Exception:
                _log.exception("%s consumer 故障；持久化输入保留", self.queue_name)
                stop.wait(0.2)
            self.ctx.queue.wait(scope, queue=self.queue_name, timeout=delay, after=revision)

    def consume(self, scope: Scope) -> None:
        # queue 只通知；未确认输入也从持久化接纳记录补取，漏通知不会丢输入。
        arrived = self.ctx.queue.drain(scope, self.queue_name)
        for item in arrived:
            self.buffers.accept(scope, item)
        for item in self.buffers.unreceived(scope, self.queue_name):
            self.apply(scope, item)
            self.buffers.receive(scope, item)

    def apply(self, scope: Scope, item: Inbound) -> None:
        pass

    def evaluate(self, scope: Scope) -> float:
        raise NotImplementedError

    def _wake(self, scope: Scope, ids: tuple[str, ...]) -> None:
        if ids:
            self.brain.wake(scope, self.queue_name, ids)


class UserConsumer(QueueConsumer):
    queue_name = QueueName.USER

    def evaluate(self, scope: Scope) -> float:
        entries = buffered(self.ctx.store.read(scope), self.queue_name)
        self._wake(scope, tuple(key for key, _ in entries))
        return _RECHECK_SECONDS


class ToolConsumer(QueueConsumer):
    queue_name = QueueName.TOOL

    def apply(self, scope: Scope, item: Inbound) -> None:
        # 工具事实由 ToolSystem 生成；queue 通知不能自行伪造调用或改写终态。
        def check(state: State) -> bool:
            invocation_id = item.payload.get("invocation_id")
            call = state["calls"].get(invocation_id) if isinstance(invocation_id, str) else None
            if (
                call
                and call["terminal"] == item.payload
                and item.source_id == call["terminal"]["event_id"]
                and item.source == "tool.terminal"
            ):
                return True
            state["inputs"][input_id(item)].update(
                phase="rejected", error="未知调用或终态与工具系统记录不一致"
            )
            return False

        if not self.ctx.store.transact(scope, "input.validated", check):
            _log.error("隔离无效工具输入 %s", input_id(item))
            try:
                self.ctx.sink.emit(
                    MessageEvent(
                        type="intake.rejected",
                        scope=scope,
                        payload={"input_id": input_id(item), "error": "无效工具终态"},
                        idempotency_key=input_id(item),
                        occur_dt=utc_now_iso(),
                    )
                )
            except Exception:
                _log.exception("无效输入通知失败；可从 session.status 查询")

    def evaluate(self, scope: Scope) -> float:
        state = self.ctx.store.read(scope)
        entries = buffered(state, self.queue_name)
        batches: dict[str, list[tuple[str, State]]] = {}
        for key, entry in entries:
            batch_id = entry["item"]["payload"]["batch_id"]
            batches.setdefault(batch_id, []).append((key, entry))
        delay = _RECHECK_SECONDS
        for batch_id, results in batches.items():
            batch = self.ctx.tools.batch_status(scope, batch_id)
            if batch is None or not batch["admitted"]:
                continue
            visible = {
                e["item"]["payload"]["invocation_id"]
                for e in state["inputs"].values()
                if e["queue"] == "tool" and e["phase"] in {"buffered", "claimed", "done"}
            }
            window = batch["buffer_window"]
            remaining = self._deadlines.remaining(
                (scope, batch_id), batch["buffer_deadline"], window
            )

            if (
                batch["presented_by_step"] is not None
                or set(batch["initial_submitted"]) <= visible
                or remaining <= 0
            ):
                self._wake(scope, tuple(key for key, _ in results))
            else:
                delay = min(delay, remaining)
        return delay


class SystemConsumer(QueueConsumer):
    queue_name = QueueName.SYSTEM

    def apply(self, scope: Scope, item: Inbound) -> None:
        if item.source in {"permission.decide", "permission.expire"}:
            self.ctx.tools.apply_permission(
                scope,
                str(item.payload["invocation_id"]),
                str(item.payload["status"]),
                input_id(item),
            )
        elif item.source == "run.cancel":

            def cancel(state: State) -> str | None:
                key = input_id(item)
                if key in state["controls"]:
                    value = state["controls"][key]["run_id"]
                    return str(value) if value else None
                run_id = item.payload.get("run_id") or state["meta"].get("run_id")
                state["controls"][key] = {"applied": True, "run_id": run_id}
                if run_id:
                    state["meta"][f"cancelled:{run_id}"] = True
                return str(run_id) if run_id else None

            run_id = self.ctx.store.transact(scope, "run.cancelled", cancel)
            if run_id:
                self.ctx.tools.cancel(scope, run_id)
        elif item.source == "step.retry":
            # 先把控制命令持久化为 applied，重放不会反复重置失败计数。
            def mark(state: State) -> bool:
                key = input_id(item)
                if key in state["controls"]:
                    return False
                state["controls"][key] = {"applied": True}
                state["meta"]["retry_seq"] = state["meta"].get("retry_seq", 0) + 1
                state["meta"]["intake_error"] = None
                active = state["meta"].get("active_step")
                if active:
                    step = state["steps"][active]
                    if step["status"] in {"failed", "paused"}:
                        step.update(status="prepared", attempts=0, error=None)
                        state["meta"].update(run_steps=0, run_started=time.time())
                return True

            self.ctx.store.transact(scope, "step.retry_requested", mark)
            self.brain.resume(scope)

    def evaluate(self, scope: Scope) -> float:
        entries = buffered(self.ctx.store.read(scope), self.queue_name)
        self._wake(
            scope, tuple(key for key, e in entries if e["item"]["source"] == "system.invoke")
        )
        return _RECHECK_SECONDS


__all__ = ["QueueConsumer", "UserConsumer", "ToolConsumer", "SystemConsumer"]
