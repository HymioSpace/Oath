"""三条队列各自收取、缓冲、决定唤醒。Brain 只看 buffer 有没有东西。"""

from __future__ import annotations

import logging
import time

from core.context.user_context import merge, table_of
from core.journal.runtime_store import State
from core.protocol.datatype import User
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.message import MessageEvent
from core.protocol.datatype.queue import QueueName
from core.protocol.interface.brain import WakeRequests
from core.runtime import AgentContext
from core.scheduling.tool_budget import exhausted, hold, reset
from core.utils.encoding import utc_now_iso

_log = logging.getLogger(__name__)
#: 没有新输入时的复查节奏。带时限的规则靠它推进 —— 工具批次的窗口就是靠这个到期。
_LISTEN_SECONDS = 5.0


def input_key(item: Inbound) -> str:
    """输入的身份。同一个身份只认领一次。"""
    return item.identity


class QueueConsumer:
    """单个用户的接纳与唤醒规则；服务负责消息交付和定时复查。"""

    queue_name: str

    def __init__(self, ctx: AgentContext, brain: WakeRequests) -> None:
        self.ctx, self.brain = ctx, brain
        #: 这一轮有没有东西进了 buffer。没进 buffer 的输入不值得叫模型。
        self._buffered = False

    def receive(self, user: User, item: Inbound) -> None:
        """一条新输入的去处。默认进 buffer 等 brain 认领。"""
        self.ctx.buffer.add(user, self.queue_name, item)
        self._buffered = True

    def evaluate(self, user: User, delivered: bool) -> float:
        """判定这一轮要不要唤醒 brain，返回下次 listen 的超时秒数。"""
        raise NotImplementedError


class UserConsumer(QueueConsumer):
    queue_name = QueueName.USER

    def receive(self, user: User, item: Inbound) -> None:
        incoming = item.payload.get("user_context")
        # 空的不写：没有新事实就别动表 —— 过期项在读的时候本来就裁掉了。
        if not isinstance(incoming, dict) or not incoming:
            super().receive(user, item)
            return

        def overlay(state: State) -> bool:
            key = "context:" + input_key(item)
            if key in state["controls"]:
                return False
            state["controls"][key] = {"applied": True}
            state["meta"]["user_context"] = merge(table_of(state), incoming, now=time.time())
            return True

        self.ctx.store.transact(user, "user_context.updated", overlay)
        super().receive(user, item)

    def evaluate(self, user: User, delivered: bool) -> float:
        if any(item.wakes for item in self.ctx.buffer.peek(user, self.queue_name)):
            self.brain.wake(user)
        return _LISTEN_SECONDS


class ToolConsumer(QueueConsumer):
    queue_name = QueueName.TOOL

    def _reject(self, user: User, item: Inbound, error: str) -> None:
        _log.info("隔离工具输入 %s: %s", input_key(item), error)
        try:
            self.ctx.sink.emit(
                MessageEvent(
                    type="intake.rejected",
                    user=user,
                    payload={"input_id": input_key(item), "error": error},
                    idempotency_key=input_key(item),
                    occur_dt=utc_now_iso(),
                )
            )
        except Exception:
            _log.exception("无效输入通知失败；可从 session.status 查询")

    def receive(self, user: User, item: Inbound) -> None:
        """工具事实由工具系统生成；队列通知不能伪造调用或改写终态。

        对不上就当场隔离，不进 buffer —— 放进去就是让模型读一条不存在的工具结果。
        顺带在同一个事务里给这一批开窗口：第一条结果进 buffer 的时刻就是窗口起点。
        """
        window = self.ctx.config.tool_round_timeout_seconds

        def check(state: State) -> str:
            invocation_id = item.payload.get("invocation_id")
            call = state["calls"].get(invocation_id) if isinstance(invocation_id, str) else None
            if not (
                call
                and call["terminal"] == item.payload
                and item.source_id == call["terminal"]["event_id"]
                and item.source == "tool.terminal"
            ):
                return "未知调用或终态不一致"
            batch = state["batches"].get(item.payload.get("batch_id"))
            if batch is not None and batch.get("buffer_deadline") is None:
                batch.update(
                    first_buffered_at=time.time(),
                    buffer_deadline=time.time() + window,
                    buffer_window=window,
                )
            return ""

        rejected = self.ctx.store.transact(user, "tool.result_verified", check)
        if rejected:
            self._reject(user, item, rejected)
            return
        super().receive(user, item)

    def evaluate(self, user: User, delivered: bool) -> float:
        """一批工具还可能再添结果时不要急着叫模型；收齐了或窗口到了再叫。

        「收齐」按**还能跑的调用**判定，不按初次提交集合：等审批的调用不占着窗口 ——
        等人不是等工具，让模型空等一个人工决定才是真的浪费。

        「叫过模型」也不等于「模型看到了」：上一轮带占位符就往下走的话，真结果到了
        仍要再叫一次，否则这次执行的结果就永远进不了上下文。

        唤醒与「窗口内先按住」是一对：按住了，窗口到期这一轮就会把它放出来。
        """
        state = self.ctx.store.read(user)
        pending = self.ctx.buffer.peek(user, self.queue_name)
        if pending and exhausted(state, self.ctx.config.max_tool_only_steps):
            self.ctx.store.transact(
                user,
                "intake.tool_budget",
                lambda current: hold(current, self.ctx.config.max_tool_only_steps),
            )
            return _LISTEN_SECONDS
        delay = _LISTEN_SECONDS
        for batch_id, batch in state["batches"].items():
            fresh = [
                i
                for i in pending
                if i.payload.get("batch_id") == batch_id and input_key(i) not in state["inputs"]
            ]
            if not fresh or not batch["admitted"]:
                continue
            visible = {i.payload.get("invocation_id") for i in pending}
            visible.update(
                entry["item"]["payload"].get("invocation_id")
                for entry in state["inputs"].values()
                if entry["queue"] == "tool"
            )
            ready = batch.get("presented_by_step") or set(batch["initial_submitted"]) <= visible
            remaining = max(0, (batch.get("buffer_deadline") or time.time()) - time.time())
            if ready or remaining <= 0:
                self.brain.wake(user)
            else:
                delay = min(delay, remaining)
        return delay


class SystemConsumer(QueueConsumer):
    queue_name = QueueName.SYSTEM

    def __init__(self, ctx: AgentContext, brain: WakeRequests) -> None:
        super().__init__(ctx, brain)
        #: 这一轮有没有收到强制唤醒。supervisor 定时往这条队列投它。
        self._forced = False

    def receive(self, user: User, item: Inbound) -> None:
        """控制命令不入 buffer；语义干预缓冲，invoke 不重置续行预算。"""
        if item.source == "system.invoke":
            # Persist wake before acknowledging the control message.
            self.brain.wake(user)
        else:
            self.apply(user, item)
            if item.source in {"permission.decide", "step.retry"}:
                self.ctx.store.transact(
                    user,
                    "intake.external_intervention",
                    lambda state: reset(state, input_key(item)),
                )
                self.brain.wake(user)
            elif item.source not in {"permission.expire", "run.cancel"}:
                super().receive(user, item)

    def apply(self, user: User, item: Inbound) -> None:
        if item.source in {"permission.decide", "permission.expire"}:
            self.ctx.tools.apply_permission(
                user,
                str(item.payload["invocation_id"]),
                str(item.payload["status"]),
                item.source_id,
                scope=str(item.payload.get("scope", "once")),
                client_id=str(item.payload.get("client_id", "")),
                channel=str(item.payload.get("channel", "unknown")),
                reason=str(item.payload.get("reason", "")),
                action_digest=str(item.payload.get("action_digest", "")),
            )
        elif item.source == "run.cancel":

            def cancel(state: State) -> str | None:
                key = input_key(item)
                if key in state["controls"]:
                    return str(state["controls"][key].get("run_id") or "") or None
                run_id = item.payload.get("run_id") or state["meta"].get("run_id")
                state["controls"][key] = {"applied": True, "run_id": run_id}
                if run_id:
                    state["meta"][f"cancelled:{run_id}"] = True
                return str(run_id) if run_id else None

            run_id = self.ctx.store.transact(user, "run.cancelled", cancel)
            if run_id:
                self.ctx.tools.cancel(user, run_id)
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

            self.ctx.store.transact(user, "step.retry_requested", mark)
            self.brain.resume(user)

    def evaluate(self, user: User, delivered: bool) -> float:
        if self._buffered or self.ctx.buffer.peek(user, self.queue_name):
            self.brain.wake(user)
        if self._forced:
            self._forced = False
            self.brain.wake(user)
        return _LISTEN_SECONDS


__all__ = ["QueueConsumer", "UserConsumer", "ToolConsumer", "SystemConsumer", "input_key"]
