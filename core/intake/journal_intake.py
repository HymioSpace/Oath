"""`JournalIntake` —— `Intake` 的 v1 实现。

入站与消费两条通路，一个实例管所有 scope：

- **入站** `handle`：把 command 翻成一条记录 append 进 journal，再 `put` 进它该在的
  队列。落盘在排队之前。
- **消费** `poll`：从三条队列各取一条，逐条问路由，送去 `Brain` 或按规则处理掉。

`run.cancel` 同样先落盘再入队 —— 它进的是 system 队列，走 `BY_RULE`。
"""

from __future__ import annotations

from core.protocol.datatype.journal import EventRecord, EventType, Record
from core.protocol.datatype.queue import QueueName
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.wire import Command, CommandType
from core.protocol.error import WireProtocolError
from core.protocol.interface.brain import Brain
from core.protocol.interface.intake import (
    EventRouter,
    Intake,
    RoutingDecision,
    SessionAuthenticator,
)
from core.runtime import AgentContext
from core.utils.encoding import JsonObject, utc_now_iso

#: command 落盘时的幂等键前缀。挂上 command id：同一个 command 重发是重放，
#: 两条不同的取消不会互相顶掉。
COMMAND_KEY_PREFIX = "cmd:"

#: 三条队列的读取顺序。`system` 排最后不是因为它不重要，而是因为每轮只从每条队列
#: 取一条 —— 顺序直接决定这一轮先应谁。
QUEUE_ORDER: tuple[str, ...] = (QueueName.USER, QueueName.TOOL, QueueName.SYSTEM)


class TypeRouter(EventRouter):
    """按 type 决定去向。

    `user.message` 与 `tool.result` 是 agent 该回应的输入，其余交给 core 自己处理。
    判断的是记录不是 command —— 一条 `run.cancel` 落盘时和被取出来时中间隔着别的
    记录，理由可能已经变了。
    """

    #: 交给 `Brain` 的 type。
    TO_BRAIN_TYPES = frozenset({EventType.USER_MESSAGE, EventType.TOOL_RESULT})

    def route(self, record: Record) -> str:
        if record.type in self.TO_BRAIN_TYPES:
            return RoutingDecision.TO_BRAIN
        return RoutingDecision.BY_RULE


class JournalIntake(Intake):
    """收 command、排队列、消费队列。"""

    def __init__(
        self,
        ctx: AgentContext,
        brain: Brain,
        router: EventRouter,
    ) -> None:
        self._ctx = ctx
        self._brain = brain
        self._router = router

    # -- 入站 --------------------------------------------------------------

    def open_session(self, command: Command, *, authenticator: SessionAuthenticator) -> Scope:
        identity = authenticator.authenticate(str(command.payload.get("credential", "")))
        return Scope(user_id=identity.user_id, session_id=identity.session_id)

    def handle(self, command: Command, scope: Scope) -> None:
        """落一条 command 并入队。抛错表示没落成，调用方不得回执成功。"""
        key = f"{COMMAND_KEY_PREFIX}{command.id}"
        if command.type == CommandType.PING:
            return
        if command.type == CommandType.USER_MESSAGE:
            self._append(
                scope,
                EventType.USER_MESSAGE,
                {"content": str(command.payload.get("content", ""))},
                key,
            )
            return
        if command.type == CommandType.RUN_CANCEL:
            payload: JsonObject = {"run_id": command.id}
            reason = command.payload.get("reason")
            if isinstance(reason, str):
                payload["reason"] = reason
            self._append(scope, EventType.SYSTEM_CANCEL, payload, key)
            return
        # 审批裁定还没进 journal（见 spec/todo.md），这条 command 办不了。
        # 抛错让传输层回一帧 error —— 静默吞掉的话，客户端点了「同意」什么也没发生，
        # 也查不出是哪一环掉的。
        raise WireProtocolError(f"{command.type} 还没有接进 journal", code="unsupported_command")

    def _append(self, scope: Scope, type_: str, payload: JsonObject, key: str) -> Record:
        """append 再入队。

        顺序不能反：队列里存的是已落盘的记录，反过来崩溃时会出现「内存里有、
        日志里查不到」的东西。

        记录是自己拼的而不是从 journal 读回来：`append` 只回游标，读回来要多扫一遍
        文件。payload 与键是刚写进去的那一份，`commit_dt` 用本地的时刻 —— 队列只读
        `type` 与 `payload`，不参与任何判定。
        """
        result = self._ctx.journal.append(
            scope, type_, payload, payload_schema_version=1, idempotency_key=key
        )
        record = Record.of(result.cursor, _raw(scope, type_, payload, key))
        self._ctx.queue.put(scope, record)
        return record

    # -- 消费 --------------------------------------------------------------

    def poll(self, scope: Scope) -> bool:
        """三条队列各取一条，逐条处理。返回 True 表示至少处理了一条。

        取一条处理一条：一次清空三条队列会让排在后面的 system 指令被一串模型
        调用饿死。
        """
        handled = False
        for queue in QUEUE_ORDER:
            record = self._ctx.queue.get(scope, queue, timeout=0)
            if record is None:
                continue
            handled = True
            self._dispatch(scope, record)
        return handled

    def _dispatch(self, scope: Scope, record: Record) -> None:
        decision = self._router.route(record)
        if decision == RoutingDecision.TO_BRAIN:
            self._brain.step(scope, record)
            return
        if decision != RoutingDecision.BY_RULE:
            raise ValueError(f"未知的路由决定: {decision!r}")
        self._apply_rule(scope, record)

    def _apply_rule(self, scope: Scope, record: Record) -> None:
        """core 自己处理掉的那一类。

        v1 只有「记一笔然后继续」：还没有 run 的概念，`system.cancel` 没有可中止的
        对象。记一条 `system.agent_step` 是为了让 trace 里看得见这次控制面指令被
        消费过，而不是被队列吞掉。
        """
        if record.type != EventType.SYSTEM_CANCEL:
            return
        run_id = str(record.payload.get("run_id", ""))
        self._ctx.journal.append(
            scope,
            EventType.SYSTEM_AGENT_STEP,
            {"step_id": run_id, "phase": "cancelled"},
            payload_schema_version=1,
            idempotency_key=f"step:{run_id}:cancelled",
        )

    def wait(self, scope: Scope, *, timeout: float | None = None) -> bool:
        return self._ctx.queue.wait(scope, timeout=timeout)

    def close(self, *, wait: bool = True) -> None:
        self._ctx.queue.close(wait=wait)
        self._brain.close(wait=wait)


def _raw(scope: Scope, type_: str, payload: JsonObject, key: str) -> EventRecord:
    return {
        "user_id": scope.user_id,
        "session_id": scope.session_id,
        "type": type_,
        "payload": payload,
        "payload_schema_version": 1,
        "idempotency_key": key,
        "occur_dt": None,
        "commit_dt": utc_now_iso(),
    }


__all__ = ["COMMAND_KEY_PREFIX", "QUEUE_ORDER", "JournalIntake", "TypeRouter"]
