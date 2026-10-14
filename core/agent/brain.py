"""`AgentBrain` —— 让「跑什么」有实现的 `Brain`。

**journal 里对话记录唯一的写入方。** 队列与 buffer 都不碰 journal：producer 不知道这一轮
是什么，落盘是读到这一批之后才有的判断。

一次 `step`：落这一批的输入 → build context → 调一次模型 → 把模型说的话落盘 → 有工具要
调就落 `tool.call` 并派发出去。**不循环、不等工具** —— 工具结果回来是下一批输入。

每一步都先落盘再做下一步。反过来做就会出现「客户端在界面上看到了、重放时这段对话
凭空消失」，那是没法查的丢数据。

## 幂等：这一批的 id 是算出来的，不是发出来的

`step_id` 由这一批输入算出，`invocation_id` 由 `step_id` 与调用序号算出。同一批输入
不管被交上来几次，算出来的都是同一组 id，journal 的幂等键因此把重放挡住：同一个命令
重发不会调第二次模型、不会派发第二次工具。

**这不只是去重。** 中途崩了之后重跑同一批，`step` 会接着上次的进度往下走：模型说过
的话已经在 journal 里，就不重新问了，直接拿它声明的调用去补 `tool.call` 与派发。

## 什么时候醒

| 这一批里有 | 行为 |
| --- | --- |
| 用户输入 | 一定处理 |
| 控制面指令 | 取消只记账不调模型；Supervisor 的唤醒照常跑一轮 |
| 工具结果，这一轮齐了 | 处理 |
| 工具结果，还没齐 | `step` 返回 `False`，buffer 不清 |
| 工具结果，到期了 | 处理，缺的那些由投影补 `unresolved` 占位 |

「这一轮派发了几个」不需要额外状态：`assistant.message` 里的 `tool_calls` 与紧随其后的
`tool.call` 记录已经落盘，扫一遍 journal 数得出来。
"""

from __future__ import annotations

import hashlib
import itertools
from datetime import UTC, datetime, timedelta

from core.protocol.datatype.cursor import Cursor, CursorKind
from core.protocol.datatype.inbound import Batch, Inbound, InboundKind
from core.protocol.datatype.journal import EventType
from core.protocol.datatype.llm import LlmRequest
from core.protocol.datatype.message import MessageEvent, MessageEventType, ToolCallRef
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.tool import ToolCallRequest
from core.protocol.datatype.wire import CommandType
from core.protocol.interface.brain import Brain
from core.protocol.interface.llm import DeltaSink, ModelProvider
from core.runtime import AgentContext
from core.utils.encoding import JsonObject, JsonValue, parse_timestamp, utc_now_iso

#: `tool.call` 幂等键的前缀。
TOOL_CALL_KEY_PREFIX = "tool.call:"
#: `tool.result` 幂等键的前缀。
RESULT_KEY_PREFIX = "tool.result:"
#: 用户输入落 `user.message` 时的幂等键前缀。挂上 command id：同一个 command 重发是
#: 重放，两条不同的消息不会互相顶掉。
COMMAND_KEY_PREFIX = "cmd:"

#: 唤醒来源。写进 `system.agent_step`，trace 里靠它分清这一轮是被谁叫醒的。
_TRIGGER_USER = "user"
_TRIGGER_TOOL = "tool"
_TRIGGER_INVOKE = "invoke"
_TRIGGER_CANCEL = "cancel"

_BEGINNING = Cursor(CursorKind.LINE, 0)


def _step_key(step_id: str, phase: str) -> str:
    return f"step:{step_id}:{phase}"


def _invocation(item: Inbound) -> str:
    return str(item.payload.get("invocation_id", ""))


def _step_id(batch: Batch) -> str:
    """这一批的 id。

    **由内容算出，不由次数发出来。** 同一批输入重跑一次要算回同一个 id，否则重放
    就变成了「又发生了一遍」：模型被问第二次、工具被派发第二次，而 journal 里躺着
    两份互相矛盾的记录。

    参与哈希的是每条输入的**身份**，不是它的正文：`invocation_id` / `command_id`。
    正文本该由 journal 的幂等指纹管，它已经做得比这里好。
    """
    digest = hashlib.sha256()
    for kind, items in (
        (InboundKind.USER, batch.user),
        (InboundKind.TOOL_RESULT, batch.tool),
        (InboundKind.INVOKE, batch.system),
    ):
        for item in items:
            digest.update(f"{kind}:{item.source_id}\n".encode())
    return digest.hexdigest()[:16]


def _invocation_id(step_id: str, index: int) -> str:
    """一次调用在这一次 step 里的标识。

    **Oath 自己的 id，不借模型的。** 模型给的 `tool_call_id` 原样透传进 journal，但
    幂等键与派发键必须是 Oath 的：模型会重发同一个 id，那不该让同一个调用跑两次。
    """
    return f"{step_id}-{index}"


class _RoundState:
    """journal 扫一遍得出的两件事。"""

    __slots__ = ("assistant", "unanswered")

    def __init__(self, unanswered: set[str], assistant: JsonObject | None) -> None:
        #: 派发出去、journal 里还没有 `tool.result` 的 `invocation_id`。
        self.unanswered = unanswered
        #: 这一步已经落盘的 `assistant.message` 的 payload。没有就是还没问过模型。
        self.assistant = assistant


class AgentBrain(Brain):
    """agent runtime 的决策方。

    **单线程假设。** 每个 session 一个消费线程，同一个实例上的 `step` 不并发跑。
    要跑多个 session 就多开线程 —— 共享状态只有 journal，而 journal 自己线程安全。
    """

    def __init__(self, ctx: AgentContext, provider: ModelProvider) -> None:
        self._ctx = ctx
        self._provider = provider
        self._closed = False

    # -- 生命周期 ----------------------------------------------------------

    def close(self, *, wait: bool = True) -> None:
        self._closed = True

    # -- 一次 step ---------------------------------------------------------

    def step(self, scope: Scope, batch: Batch) -> bool:
        """处理一批输入，把它带来的下一步做完。

        返回 `False` 表示现在还不该处理 —— 典型是这一轮工具结果还没齐。调用方据此不清
        buffer，下一次醒来还会拿到同一批。

        模型调用失败就抛 —— buffer 里那批东西没动过，下一轮还会被交上来。
        """
        if self._closed:
            # 关掉之后 journal 随时会跟着关。这条线程醒来时不再碰它 —— 让它在
            # `_emit` 或 `append` 上炸，比写进一份正在被拆的日志强。
            return False

        step_id = _step_id(batch)
        state = self._scan(scope, step_id)
        trigger = self._trigger(batch, state)
        if trigger is None:
            return False

        try:
            # 先落这批输入，再记「这一步开始了」—— trace 里读到的顺序跟事情
            # 发生的顺序一致：先看见了什么，才是被什么叫醒的。
            self._append_inputs(scope, batch)
            self._record_step(scope, step_id, "start", trigger)
            if trigger != _TRIGGER_CANCEL:
                self._think(scope, step_id, state)
        except Exception:
            self._record_step(scope, step_id, "error", trigger)
            raise
        self._record_step(scope, step_id, "end", trigger)
        return True

    def wakeup_delay(self, scope: Scope, batch: Batch) -> float | None:
        """这一批还要等多久才处理。

        `None` 表示纯等新输入进来。工具结果不齐的那一支才返回秒数：那些没回来的工具
        不会往队列里 `put` 任何东西，没有别的办法把它们叫醒。
        """
        if not batch.tool or batch.user or batch.system:
            return None
        arrived = {_invocation(item) for item in batch.tool}
        unanswered = self._scan(scope, _step_id(batch)).unanswered
        if not unanswered or unanswered <= arrived:
            return None
        return self._seconds_left(batch.tool)

    def _trigger(self, batch: Batch, state: _RoundState) -> str | None:
        """这批算不算一次输入。返回唤醒来源的名字，不算就返回 None。"""
        if batch.user:
            return _TRIGGER_USER
        if batch.system:
            # 取消没有可中止的对象 —— 记一笔就结束，不需要调模型。Supervisor 的定时
            # 唤醒才要走完整的一轮。
            if all(item.source == CommandType.RUN_CANCEL for item in batch.system):
                return _TRIGGER_CANCEL
            return _TRIGGER_INVOKE
        if batch.tool and (_settled(state, batch.tool) or self._expired(batch.tool)):
            return _TRIGGER_TOOL
        return None

    def _think(self, scope: Scope, step_id: str, state: _RoundState) -> None:
        # 问过就别再问一次 —— 「问过了但一个工具都不调」也是问过了，靠 `assistant`
        # 在不在来分辨，靠「有没有 tool_calls」分辨不了。
        refs = self._ask(scope, step_id) if state.assistant is None else self._refs_from(state)
        if refs:
            self._ctx.tools.execute(scope, self._append_tool_calls(scope, refs, step_id))

    def _ask(self, scope: Scope, step_id: str) -> tuple[ToolCallRef, ...]:
        context = self._ctx.context_manager.build(scope, self._ctx.tools.definitions())
        response = self._provider.complete(
            LlmRequest(
                messages=context.messages,
                tools=context.tools,
                model=self._ctx.config.provider.model,
                max_output_tokens=self._ctx.config.max_output_tokens,
                temperature=self._ctx.config.temperature,
                extra_params=self._ctx.config.provider.extra_params,
            ),
            on_delta=self._delta_sink(scope, step_id),
        )
        refs = tuple(
            ToolCallRef(id=c.tool_call_id, name=c.tool, arguments=c.arguments)
            for c in response.tool_calls
        )
        self._append_assistant(
            scope, step_id, response.content, response.reasoning, refs, response.usage
        )
        return refs

    def _refs_from(self, state: _RoundState) -> tuple[ToolCallRef, ...]:
        """已经落盘的 `assistant.message` 声明了哪些调用。

        重跑一步时模型不必再问一遍 —— 说过的话在 journal 里，那才是这一轮真正说过的话。
        """
        if state.assistant is None:
            return ()
        raw: JsonValue = state.assistant.get("tool_calls")
        if not isinstance(raw, list):
            return ()
        refs = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            arguments = item.get("arguments")
            refs.append(
                ToolCallRef(
                    id=str(item.get("id", "")),
                    name=str(item.get("name", "")),
                    arguments=dict(arguments) if isinstance(arguments, dict) else {},
                )
            )
        return tuple(refs)

    def _delta_sink(self, scope: Scope, step_id: str) -> DeltaSink:
        """把一段增量变成一条 `assistant.delta` 事件。

        **不落盘。** 一段增量在 journal 里是一句半话，append 它等于让记录说谎。
        重放时靠完整的 `assistant.message` 兜底 —— 它才是那一轮真正说过的话。
        """
        counter = itertools.count(1)

        def on_delta(channel: str, text: str) -> None:
            seq = next(counter)
            self._emit(
                MessageEvent(
                    type=MessageEventType.DELTA,
                    scope=scope,
                    payload={
                        "step_id": step_id,
                        "channel": channel,
                        "text": text,
                        "seq": seq,
                    },
                    idempotency_key=f"delta:{step_id}:{seq}",
                    occur_dt=utc_now_iso(),
                )
            )

        return on_delta

    # -- 这一批的输入 ------------------------------------------------------

    def _append_inputs(self, scope: Scope, batch: Batch) -> None:
        """落这一批。**这是这些事实在 journal 里第一次出现的地方。**

        每条输入落成它自己那一条记录，一条不多一条不少。用户输入就是用户说的话，
        工具结果就是工具回的东西 —— 迟到的那个也一样落 `tool.result`，要不要换个说法
        告诉模型是投影的事。落的时候顺手做一次判断，就把「这条记录长什么样」和「这一
        批凑巧带上了什么」绑在了一起：同一句话配不配得上同一组工具结果，重放一次
        就不一定一样。
        """
        for item in batch.user:
            self._append_user(scope, item)
        for item in batch.tool:
            self._ctx.journal.append(
                scope,
                EventType.TOOL_RESULT,
                item.payload,
                payload_schema_version=1,
                idempotency_key=f"{RESULT_KEY_PREFIX}{_invocation(item)}",
                occur_dt=item.occur_dt or None,
            )
        for item in batch.system:
            self._append_control(scope, item)

    def _append_user(self, scope: Scope, item: Inbound) -> None:
        """用户输入不投递客户端 —— 发出去的 `user.message` 没有回声，客户端自己存。

        `occur_dt` 是用户按下去的时刻，不是落盘的时刻：两件事之间隔着攒批和等工具，
        用落盘时刻会把「用户等了多久」这件事从日志里抹掉。
        """
        self._ctx.journal.append(
            scope,
            EventType.USER_MESSAGE,
            {"content": _content(item)},
            payload_schema_version=1,
            idempotency_key=f"{COMMAND_KEY_PREFIX}{item.source_id}",
            occur_dt=item.occur_dt or None,
        )

    def _append_control(self, scope: Scope, item: Inbound) -> None:
        """控制面指令：原样落成它对应的那条记录。

        `payload` 由 `of_command` 按 `system.cancel` 的 schema 拼好，这里不翻译 ——
        翻译一次就多一处两边的字段名可能对不上的地方。
        """
        if item.source != CommandType.RUN_CANCEL:
            return
        self._ctx.journal.append(
            scope,
            EventType.SYSTEM_CANCEL,
            item.payload,
            payload_schema_version=1,
            idempotency_key=f"{COMMAND_KEY_PREFIX}{item.source_id}",
            occur_dt=item.occur_dt or None,
        )

    # -- 这一轮的形状 ------------------------------------------------------

    def _scan(self, scope: Scope, step_id: str) -> _RoundState:
        """扫一遍 journal：这一轮还差谁，以及这一步已经说过什么。

        单次扫描，两件事共用：问的是同一批记录，拆成两次扫就多一倍的读盘。
        """
        key = f"msg:{step_id}"
        unanswered: set[str] = set()
        assistant: JsonObject | None = None
        for record in self._ctx.journal.read_range(scope, _BEGINNING):
            type_ = record.type
            if type_ == EventType.TOOL_CALL:
                unanswered.add(str(record.payload.get("invocation_id", "")))
            elif type_ == EventType.TOOL_RESULT:
                unanswered.discard(str(record.payload.get("invocation_id", "")))
            elif type_ == EventType.ASSISTANT_MESSAGE and record.idempotency_key == key:
                assistant = record.payload
        return _RoundState(unanswered, assistant)

    def _seconds_left(self, items: tuple[Inbound, ...]) -> float:
        """从**第一个结果到达**起算还剩几秒。

        不是从派发起算：慢的是工具，不是 agent 决定等它。第一个结果落地之后仍然等满
        剩下的时间，是给同一批里还在跑的那几个留的最后一段宽限。
        """
        first = min(_occurred_at(item) for item in items)
        deadline = first + timedelta(seconds=self._ctx.config.tool_round_timeout_seconds)
        return max(0.0, (deadline - datetime.now(UTC)).total_seconds())

    def _expired(self, items: tuple[Inbound, ...]) -> bool:
        """等够了没有。

        没回来的那几个不会往队列里 `put` 任何东西，也没有别的办法把它们叫醒 ——
        没有这一条，这一轮会永远停在 buffer 里，agent 也不再说话。
        """
        return self._seconds_left(items) <= 0.0

    # -- 模型的产物 --------------------------------------------------------

    def _append_assistant(
        self,
        scope: Scope,
        step_id: str,
        content: str,
        reasoning: str | None,
        refs: tuple[ToolCallRef, ...],
        usage: JsonObject,
    ) -> None:
        """assistant 那一轮先落盘。

        它带着 `tool_calls`：投影靠这些调用给每个槽位占位，`tool.result` 回来时才有
        地方填。
        """
        payload: JsonObject = {"content": content, "reasoning": reasoning}
        if refs:
            payload["tool_calls"] = [ref.to_dict() for ref in refs]
        if usage:
            payload["usage"] = dict(usage)
        if self._ctx.config.provider.model:
            payload["model"] = self._ctx.config.provider.model

        key = f"msg:{step_id}"
        result = self._ctx.journal.append(
            scope,
            EventType.ASSISTANT_MESSAGE,
            payload,
            payload_schema_version=1,
            idempotency_key=key,
        )
        if result.idempotent_hit:
            return
        self._emit(
            MessageEvent(
                type=MessageEventType.ASSISTANT_MESSAGE,
                scope=scope,
                payload=payload,
                idempotency_key=key,
                occur_dt=utc_now_iso(),
            )
        )

    def _append_tool_calls(
        self, scope: Scope, refs: tuple[ToolCallRef, ...], step_id: str
    ) -> list[ToolCallRequest]:
        """每个 tool call 落一条 `tool.call`，并凑出派发请求。

        `invocation_id` 由 `step_id` 与序号算出，写进落盘记录、也交给工具系统 ——
        同一个值，不从游标反推、不从模型给的 id 借用：派发键必须是 Oath 自己的。
        它还必须是**算得出来的**：重跑一步要拿回同一个 id，工具系统才会把这次派发
        认成重复而不是又跑一遍。
        """
        requests: list[ToolCallRequest] = []
        for index, ref in enumerate(refs):
            invocation_id = _invocation_id(step_id, index)
            payload: JsonObject = {
                "tool_call_id": ref.id,
                "invocation_id": invocation_id,
                "tool": ref.name,
                "arguments": ref.arguments,
            }
            key = f"{TOOL_CALL_KEY_PREFIX}{invocation_id}"
            result = self._ctx.journal.append(
                scope, EventType.TOOL_CALL, payload, payload_schema_version=1, idempotency_key=key
            )
            if not result.idempotent_hit:
                self._emit(
                    MessageEvent(
                        type=MessageEventType.TOOL_CALL,
                        scope=scope,
                        tool_call_id=ref.id,
                        invocation_id=invocation_id,
                        tool=ref.name,
                        payload=payload,
                        idempotency_key=key,
                        occur_dt=utc_now_iso(),
                    )
                )
            requests.append(
                ToolCallRequest(
                    tool=ref.name,
                    arguments=ref.arguments,
                    tool_call_id=ref.id,
                    invocation_id=invocation_id,
                )
            )
        return requests

    def _record_step(self, scope: Scope, step_id: str, phase: str, trigger: str) -> None:
        """记一条 `system.agent_step`。

        `trigger` 是这一轮是被什么叫醒的 —— 没有它，trace 里分不清「用户问了」和
        「工具回来了」，两者的排查方式不一样。

        它是审计记录，不投递给客户端：消费方是 trace 视图，不是 agent 的对端。
        """
        self._ctx.journal.append(
            scope,
            EventType.SYSTEM_AGENT_STEP,
            {"step_id": step_id, "phase": phase, "trigger": trigger},
            payload_schema_version=1,
            idempotency_key=_step_key(step_id, phase),
        )

    # -- 投递 --------------------------------------------------------------

    def _emit(self, event: MessageEvent) -> None:
        try:
            self._ctx.sink.emit(event)
        except Exception:
            # 送不出去就算了 —— 事件已经落盘，对话不会缺，只是客户端没看见。
            # 这一版不重试：重试只会反复失败。工具系统的终态事件不是这样处理的，
            # 那边的失败会让结果变成 `outcome_unknown`，两边不一样是有原因的。
            return


def _settled(state: _RoundState, items: tuple[Inbound, ...]) -> bool:
    """这一轮的工具结果齐了吗。"""
    if not state.unanswered:
        return True
    return state.unanswered <= {_invocation(item) for item in items}


def _content(item: Inbound) -> str:
    return str(item.payload.get("content", ""))


def _occurred_at(item: Inbound) -> datetime:
    if not item.occur_dt:
        return datetime.now(UTC)
    try:
        return parse_timestamp(item.occur_dt)
    except ValueError:
        return datetime.now(UTC)


__all__ = [
    "COMMAND_KEY_PREFIX",
    "RESULT_KEY_PREFIX",
    "TOOL_CALL_KEY_PREFIX",
    "AgentBrain",
]
