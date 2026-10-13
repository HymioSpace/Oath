"""`AgentBrain` —— 让「跑什么」有实现的 `Brain`。

一次 `step` 就是：恢复 context → 调一次模型 → 把模型说的话落盘 → 有工具要调就落
`tool.call` 并派发出去。**不循环、不等工具** —— 工具结果回来是一条新的输入，
由下一次 `step` 接上。

每一步都先落盘再做下一步。反过来做就会出现「客户端在界面上看到了、重放时这段对话
凭空消失」，那是没法查的丢数据。
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable

from core.protocol.datatype.journal import EventType, Record
from core.protocol.datatype.llm import LlmRequest
from core.protocol.datatype.message import MessageEvent, MessageEventType, ToolCallRef
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.tool import ToolCallRequest
from core.protocol.interface.brain import Brain
from core.protocol.interface.llm import DeltaSink, ModelProvider
from core.runtime import AgentContext
from core.utils.encoding import JsonObject, utc_now_iso

#: `tool.call` 幂等键的前缀。
TOOL_CALL_KEY_PREFIX = "tool.call:"


def _step_key(step_id: str, phase: str) -> str:
    return f"step:{step_id}:{phase}"


def _default_id_gen() -> Callable[[], str]:
    counter = itertools.count(1)
    lock = threading.Lock()

    def gen() -> str:
        with lock:
            return f"{next(counter):08x}"

    return gen


class AgentBrain(Brain):
    """agent runtime 的决策方。

    **单线程假设。** 每个 session 一个消费线程，同一个实例上的 `step` 不并发跑。
    要跑多个 session 就多开线程 —— 共享状态只有 journal，而 journal 自己线程安全。
    """

    def __init__(
        self,
        ctx: AgentContext,
        provider: ModelProvider,
        *,
        id_gen: Callable[[], str] | None = None,
    ) -> None:
        self._ctx = ctx
        self._provider = provider
        # id 源是注入的：测试要能定住它，否则幂等键每次都不一样，
        # 「重放不写新行」这条根本验不了。
        self._next_id = id_gen or _default_id_gen()
        self._closed = False

    # -- 生命周期 ----------------------------------------------------------

    def close(self, *, wait: bool = True) -> None:
        self._closed = True

    # -- 一次 step ---------------------------------------------------------

    def step(self, scope: Scope, record: Record) -> None:
        """处理一条输入，把它带来的下一步做完。

        模型调用失败就抛 —— 这条记录已经从队列取出，不会自动重来。抛出去让
        `Intake` 记 `system.agent_step{error}`，客户端看得到发生了什么。
        """
        step_id = self._next_id()
        self._record_step(scope, step_id, "start")
        try:
            self._think(scope, step_id)
        except Exception:
            self._record_step(scope, step_id, "error")
            raise
        self._record_step(scope, step_id, "end")

    def _think(self, scope: Scope, step_id: str) -> None:
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
        if refs:
            self._ctx.tools.execute(scope, self._append_tool_calls(scope, refs))

    def _delta_sink(self, scope: Scope, step_id: str) -> DeltaSink:
        """把一段增量变成一条 `assistant.delta` 事件。

        **不落盘。** 一段增量在 journal 里是一句半话，append 它等于让记录说谎。
        重放时靠完整的 `assistant.message` 兜底 —— 它才是那一轮真正说过的话。

        幂等键带自增序号而不是内容：delta 不参与幂等（同一次调用重跑，靠
        `msg:<step_id>` 去重就够了），但它要能说明「这是这一轮的第几段」，
        排查时对得上。
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

    # -- 落盘 --------------------------------------------------------------

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

        它带着 `tool_calls`：投影靠这条记录里的 `invocation_id` 给每个工具调用占位，
        `tool.result` 回来时才有地方填。
        """
        payload: JsonObject = {"content": content, "reasoning": reasoning}
        if refs:
            payload["tool_calls"] = [ref.to_dict() for ref in refs]
        if usage:
            payload["usage"] = dict(usage)
        if self._ctx.config.provider.model:
            payload["model"] = self._ctx.config.provider.model

        key = f"msg:{step_id}"
        self._ctx.journal.append(
            scope,
            EventType.ASSISTANT_MESSAGE,
            payload,
            payload_schema_version=1,
            idempotency_key=key,
        )
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
        self, scope: Scope, refs: tuple[ToolCallRef, ...]
    ) -> list[ToolCallRequest]:
        """每个 tool call 落一条 `tool.call`，并凑出派发请求。

        `invocation_id` 在这里生成，写进落盘记录、也交给工具系统 —— 同一个值，
        不从游标反推、不从模型给的 id 借用：派发键必须是 Oath 自己的。
        """
        requests: list[ToolCallRequest] = []
        for ref in refs:
            invocation_id = self._next_id()
            payload: JsonObject = {
                "tool_call_id": ref.id,
                "invocation_id": invocation_id,
                "tool": ref.name,
                "arguments": ref.arguments,
            }
            key = f"{TOOL_CALL_KEY_PREFIX}{invocation_id}"
            self._ctx.journal.append(
                scope, EventType.TOOL_CALL, payload, payload_schema_version=1, idempotency_key=key
            )
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

    def _record_step(self, scope: Scope, step_id: str, phase: str) -> None:
        """记一条 `system.agent_step`。

        它是审计记录，不投递给客户端：消费方是 trace 视图，不是 agent 的对端。
        """
        self._ctx.journal.append(
            scope,
            EventType.SYSTEM_AGENT_STEP,
            {"step_id": step_id, "phase": phase},
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


__all__ = ["TOOL_CALL_KEY_PREFIX", "AgentBrain"]
