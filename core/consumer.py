"""三条队列各自收取、缓冲、决定唤醒。Brain 只看 buffer 有没有东西。"""

from __future__ import annotations

import logging
import threading
import time

from core.context.user_context import merge, table_of
from core.journal.runtime_store import State
from core.protocol.datatype import Scope
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.message import MessageEvent
from core.protocol.datatype.queue import QueueName
from core.protocol.interface.brain import Brain
from core.runtime import AgentContext
from core.tool.lifecycle import TERMINAL
from core.utils.deadlines import Deadlines
from core.utils.encoding import utc_now_iso

_log = logging.getLogger(__name__)
#: 没有新输入时的复查节奏。带时限的规则靠它推进 —— 工具批次的窗口就是靠这个到期。
_LISTEN_SECONDS = 5.0


def input_key(item: Inbound) -> str:
    """输入的身份。同一个身份只认领一次。"""
    return item.identity


class QueueConsumer:
    """一条队列一个线程。

    循环只有四步：从队列取走新输入、放进 buffer、判定要不要唤醒、等着。判定规则由子类给，
    返回值是下次等多久 —— 谁都没给东西的时候也照常复查，否则带时限的规则永远不触发。
    """

    queue_name: str

    def __init__(self, ctx: AgentContext, brain: Brain) -> None:
        self.ctx, self.brain = ctx, brain
        self._deadlines = Deadlines()
        #: 这一轮有没有东西进了 buffer。没进 buffer 的输入不值得叫模型。
        self._buffered = False

    def run(self, scope: Scope, stop: threading.Event) -> None:
        while not stop.is_set():
            self._buffered = False
            try:
                arrived = self.deliver(scope)
                timeout = self.evaluate(scope, arrived)
                # **listen 也在这个 try 里。** 中间件挂掉时它抛的异常同样是异常，放到
                # try 外面就是让线程直接退出：这个进程还在、还在回心跳、还在 ACK，
                # 但再也不会消费任何输入 —— supervisor 看的是进程，看不见这件事。
                self.ctx.queue.listen(scope, self.queue_name, timeout)
            except Exception:
                _log.exception("%s consumer 故障；已缓冲的输入保留", self.queue_name)
                stop.wait(0.2)

    def deliver(self, scope: Scope) -> bool:
        """把新到的输入收进 buffer。返回收到了没有。

        **写进 buffer 一条就确认队列那一条。** 整批取完再整批确认等于承认没落盘的那几条
        也已经收下了 —— 中途失败的话，它们从此谁都不再管。
        """
        arrived = self.ctx.queue.get_all(scope, self.queue_name)
        for item in arrived:
            self.receive(scope, item)
            self.ctx.queue.confirm(scope, self.queue_name, item)
        return bool(arrived)

    def receive(self, scope: Scope, item: Inbound) -> None:
        """一条新输入的去处。默认进 buffer 等 brain 认领。"""
        self.ctx.buffer.add(scope, self.queue_name, item)
        self._buffered = True

    def evaluate(self, scope: Scope, delivered: bool) -> float:
        """判定这一轮要不要唤醒 brain，返回下次 listen 的超时秒数。"""
        raise NotImplementedError


class UserConsumer(QueueConsumer):
    queue_name = QueueName.USER

    def receive(self, scope: Scope, item: Inbound) -> None:
        incoming = item.payload.get("user_context")
        # 空的不写：没有新事实就别动表 —— 过期项在读的时候本来就裁掉了。
        if not isinstance(incoming, dict) or not incoming:
            super().receive(scope, item)
            return

        def overlay(state: State) -> bool:
            state["meta"]["user_context"] = merge(table_of(state), incoming, now=time.time())
            return True

        self.ctx.store.transact(scope, "user_context.updated", overlay)
        super().receive(scope, item)

    def evaluate(self, scope: Scope, delivered: bool) -> float:
        if self._buffered:
            self.brain.wake(scope)
        return _LISTEN_SECONDS


class ToolConsumer(QueueConsumer):
    queue_name = QueueName.TOOL

    def __init__(self, ctx: AgentContext, brain: Brain) -> None:
        super().__init__(ctx, brain)
        #: 每个批次已经收进 buffer 的结果。进程内记账，用来判断一批是否收齐。
        self._received: dict[tuple[Scope, str], set[str]] = {}

    def receive(self, scope: Scope, item: Inbound) -> None:
        """工具事实由工具系统生成；队列通知不能伪造调用或改写终态。

        对不上就当场隔离，不进 buffer —— 放进去就是让模型读一条不存在的工具结果。
        顺带在同一个事务里给这一批开窗口：第一条结果进 buffer 的时刻就是窗口起点。
        """
        window = self.ctx.config.tool_round_timeout_seconds

        def check(state: State) -> bool:
            invocation_id = item.payload.get("invocation_id")
            call = state["calls"].get(invocation_id) if isinstance(invocation_id, str) else None
            if not (
                call
                and call["terminal"] == item.payload
                and item.source_id == call["terminal"]["event_id"]
                and item.source == "tool.terminal"
            ):
                return False
            batch = state["batches"].get(item.payload.get("batch_id"))
            if batch is not None and batch.get("buffer_deadline") is None:
                batch.update(
                    first_buffered_at=time.time(),
                    buffer_deadline=time.time() + window,
                    buffer_window=window,
                )
            return True

        if not self.ctx.store.transact(scope, "tool.result_verified", check):
            _log.error("隔离无效工具输入 %s", input_key(item))
            try:
                self.ctx.sink.emit(
                    MessageEvent(
                        type="intake.rejected",
                        scope=scope,
                        payload={"input_id": input_key(item), "error": "未知调用或终态不一致"},
                        idempotency_key=input_key(item),
                        occur_dt=utc_now_iso(),
                    )
                )
            except Exception:
                _log.exception("无效输入通知失败；可从 session.status 查询")
            return
        batch_id = str(item.payload["batch_id"])
        self._received.setdefault((scope, batch_id), set()).add(str(item.payload["invocation_id"]))
        super().receive(scope, item)

    def evaluate(self, scope: Scope, delivered: bool) -> float:
        """一批工具还可能再添结果时不要急着叫模型；收齐了或窗口到了再叫。

        「收齐」按**还能跑的调用**判定，不按初次提交集合：等审批的调用不占着窗口 ——
        等人不是等工具，让模型空等一个人工决定才是真的浪费。

        「叫过模型」也不等于「模型看到了」：上一轮带占位符就往下走的话，真结果到了
        仍要再叫一次，否则这次执行的结果就永远进不了上下文。

        唤醒与「窗口内先按住」是一对：按住了，窗口到期这一轮就会把它放出来。
        """
        state = self.ctx.store.read(scope)
        delay = _LISTEN_SECONDS
        for batch_id, batch in state["batches"].items():
            if not batch["admitted"] or batch.get("delivered"):
                self._received.pop((scope, batch_id), None)
                continue
            deadline = batch.get("buffer_deadline")
            if deadline is None:
                continue
            members = [state["calls"][i] for i in batch["members"]]
            settled = not any(c["status"] in {"queued", "running"} for c in members)
            settled = settled and self._received.get((scope, batch_id), set()) >= {
                c["invocation_id"] for c in members if c["status"] in TERMINAL
            }
            remaining = self._deadlines.remaining(
                (scope, batch_id), deadline, batch["buffer_window"]
            )
            if settled or remaining <= 0:
                self.brain.wake(scope)
            else:
                delay = min(delay, remaining)
        return delay


class SystemConsumer(QueueConsumer):
    queue_name = QueueName.SYSTEM

    def __init__(self, ctx: AgentContext, brain: Brain) -> None:
        super().__init__(ctx, brain)
        #: 这一轮有没有收到强制唤醒。supervisor 定时往这条队列投它。
        self._forced = False

    def receive(self, scope: Scope, item: Inbound) -> None:
        """控制命令当场生效；`system.invoke` 只是强制唤醒。

        两者都不进 buffer —— 没有模型要读它们，它们要的是立刻起作用。
        """
        if item.source == "system.invoke":
            self._forced = True
        else:
            self.apply(scope, item)

    def apply(self, scope: Scope, item: Inbound) -> None:
        if item.source in {"permission.decide", "permission.expire"}:
            self.ctx.tools.apply_permission(
                scope,
                str(item.payload["invocation_id"]),
                str(item.payload["status"]),
                item.source_id,
            )
        elif item.source == "run.cancel":

            def cancel(state: State) -> str | None:
                key = input_key(item)
                if key in state["controls"]:
                    return None
                run_id = item.payload.get("run_id") or state["meta"].get("run_id")
                state["controls"][key] = {"applied": True, "run_id": run_id}
                if run_id:
                    state["meta"][f"cancelled:{run_id}"] = True
                return str(run_id) if run_id else None

            run_id = self.ctx.store.transact(scope, "run.cancelled", cancel)
            if run_id:
                self.ctx.tools.cancel(scope, run_id)
        elif item.source == "step.retry":

            def mark(state: State) -> bool:
                # 控制命令持久化为 applied，重放不会反复重置失败计数。
                key = input_key(item)
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

    def evaluate(self, scope: Scope, delivered: bool) -> float:
        if self._forced:
            self._forced = False
            self.brain.wake(scope)
        return _LISTEN_SECONDS


__all__ = ["QueueConsumer", "UserConsumer", "ToolConsumer", "SystemConsumer", "input_key"]
