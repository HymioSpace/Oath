"""每个 session 串行执行；消费者和工具 worker 不被模型请求阻塞。"""

from __future__ import annotations

import copy
import itertools
import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import cast

from core.compaction.coordinator import CompactionCoordinator
from core.intake import IntakeCoordinator
from core.journal.runtime_store import State
from core.protocol.datatype import Message, ToolCallRequest, ToolDefinition, User
from core.protocol.datatype.llm import LlmRequest, LlmResponse
from core.protocol.datatype.message import MessageEvent, MessageEventType
from core.protocol.datatype.trace import (
    SPAN_ID_LENGTH,
    Span,
    SpanEvent,
    SpanKind,
    SpanLink,
    SpanStatus,
)
from core.protocol.interface.llm import ModelProvider
from core.runtime import AgentContext
from core.shell import workspace_note
from core.trace.attributes import (
    BODY,
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MAX_TOKENS,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_REQUEST_TEMPERATURE,
    GEN_AI_RESPONSE_FINISH_REASONS,
    GEN_AI_USAGE_CACHE_READ_TOKENS,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    OATH_LLM_ATTEMPT,
    OATH_LLM_DURATION_MS,
    OATH_LLM_FIRST_CONTENT_MS,
    OATH_LLM_TPOT_MS,
    OATH_LLM_TTFT_MS,
    OATH_RUN_ID,
    OATH_STEP_DURATION_MS,
    OATH_STEP_ID,
    OATH_STEP_INPUT_COUNT,
    OATH_STEP_LLM_CALLS,
    OATH_STEP_STATUS,
    OATH_STEP_TOOL_CALLS,
    OATH_STEP_TRIGGERS,
    OP_CHAT,
    OP_INVOKE_AGENT,
    REQUEST_EVENT,
    RESPONSE_EVENT,
    STAGE_COMPLETED,
    STAGE_REQUEST_PREPARED,
    STAGE_TOOLS_SUBMITTED,
    chat_span_name,
    input_event_name,
)
from core.trace.links import source_links
from core.utils.encoding import JsonObject, canonical_json, utc_now_iso
from core.utils.tokens import estimate_text

_log = logging.getLogger(__name__)


class AgentBrain:
    def __init__(
        self, ctx: AgentContext, provider: ModelProvider, compaction: CompactionCoordinator
    ) -> None:
        self._ctx, self._provider = ctx, provider
        self._store = ctx.store
        self._compaction = compaction
        self._lock = threading.Lock()
        self._stages: dict[str, dict[str, SpanEvent]] = {}
        self._stop = threading.Event()
        self.intake = IntakeCoordinator(ctx, self, self._stop)

    def system_prompt(self) -> str:
        """system prompt + 工作区 + 技能索引。模型先读规则，再知道在哪干活、有什么可选能力。

        两段都是启动时的快照，每一步的请求照常被冻结：工作区变了也要等下次启动。
        """
        from core.agent.runtime_prompt import RUNTIME_PROMPT
        parts = [
            self._ctx.config.system_prompt,
            RUNTIME_PROMPT,
            workspace_note(self._ctx.config.workspace_root)
            if self._ctx.config.builtin_bash
            else "",
            self._ctx.skills.list(self._ctx.config.skill_index_tokens),
        ]
        return "\n\n".join(part for part in parts if part)

    def _prepare_request(self, user: User, step: State) -> State:
        if step["request"] is not None:
            return step
        draft = step["request_draft"]
        messages = draft["messages"]
        # system prompt 已在 draft；Coordinator 负责加入固定开销，不能重复计入。
        overhead = estimate_text(canonical_json(draft["tools"])) + draft["max_output_tokens"]
        prompt = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        self._compaction.prepare(
            user, overhead=overhead, system_prompt=prompt, fence=self._store.fence
        )
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

        prepared = self._store.transact(user, "step.request", freeze)
        # 压缩判定可能同步调一次模型，所以这个时刻之前的时间都算「模型之前」。
        self._stage(
            user,
            step["step_id"],
            STAGE_REQUEST_PREPARED,
            {OATH_STEP_INPUT_COUNT: len(prepared["request"]["messages"])},
        )
        return prepared

    def execute(self, user: User, step: State) -> None:
        if step["response"] is None:
            step = self._prepare_request(user, step)
        if self._stop.is_set():
            return
        step_id = step["step_id"]
        if step["response"] is None:
            if step["attempts"] >= self._ctx.config.llm_max_attempts:
                self._fail(user, step_id, "模型重试次数已耗尽")
                return

            def attempt(state: State) -> None:
                state["steps"][step_id]["attempts"] += 1

            self._store.transact(user, "step.attempt", attempt)
            attempt_no = int(step["attempts"]) + 1
            emit = self._delta(user, step_id)
            # ttft = 第一段增量到达的时刻。计时用单调时钟：墙上时间会被 NTP 拨一下。
            timing: list[float] = []
            content_timing: list[float] = []
            clock = time.monotonic()

            def on_delta(channel: str, text: str) -> None:
                if not timing:
                    timing.append(time.monotonic())
                if channel == "content" and not content_timing:
                    content_timing.append(time.monotonic())
                emit(channel, text)

            started_ns = time.time_ns()
            try:
                response = self._provider.complete(_request(step["request"]), on_delta=on_delta)
                if self._stop.is_set():
                    return
                payload = _response(response)
            except Exception as exc:
                self._record_llm(user, step, attempt_no, started_ns, clock, timing, None, str(exc))
                self._fail(user, step_id, str(exc))
                return
            self._record_llm(
                user, step, attempt_no, started_ns, clock, timing, response, None, content_timing
            )

            def save(state: State) -> None:
                state["steps"][step_id]["response"] = payload

            self._store.transact(user, "step.response", save)
            step["response"] = payload
        # 已保存响应后只补后处理；不重新调用模型。
        cancelled = self._store.read(user)["meta"].get(f"cancelled:{step['run_id']}")
        calls = [] if cancelled else step["response"]["tool_calls"]
        if calls:
            self._ctx.tools.submit_batch(
                user,
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
            self._stage(user, step_id, STAGE_TOOLS_SUBMITTED, {OATH_STEP_TOOL_CALLS: len(calls)})

        def finish(state: State) -> State:
            current = state["steps"][step_id]
            if current["status"] == "completed":
                return cast(State, current)
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
            # 没有工具调用就是这一串的最后一个 step：下一个 step 开新 trace。
            state["meta"]["trace_step"] = step_id if calls else None
            return cast(State, current)

        # 终态从事务里回读，不复用认领时那份 —— 写进 span 的必须是最终的 status。
        closed = self._store.transact(user, "step.completed", finish)
        self._stage(user, step_id, STAGE_COMPLETED, {OATH_STEP_TOOL_CALLS: len(calls)})
        self._close_step(user, closed, SpanStatus.OK, int(closed["attempts"]))

    def _fail(self, user: User, step_id: str, error: str) -> None:
        def change(state: State) -> State:
            step = state["steps"][step_id]
            failed = step["attempts"] >= self._ctx.config.llm_max_attempts
            step.update(status="failed" if failed else "prepared", error=error[:500])
            return cast(State, step)

        step = self._store.transact(user, "step.error", change)
        if step["status"] == "failed":
            self._close_step(user, step, SpanStatus.ERROR, int(step["attempts"]))
            return
        self._stop.wait(self._ctx.config.llm_retry_delay)

    # -- trace ---------------------------------------------------------------

    def _trace(self, user: User, span: Span) -> None:
        """写一条 span。**写失败不上抛**：trace 是派生的，journal 才是事实来源。"""
        try:
            self._ctx.traces.append(user, span)
        except Exception:
            _log.exception("写 trace 失败，推理不受影响")

    def open_step(
        self,
        user: User,
        step: State,
        claimed: list[tuple[str, float | None, State]],
        links: tuple[SpanLink, ...],
    ) -> None:
        """step 认领时的第一条 span。`end` 为空 —— 进程崩在这一步中途就留个空 end。"""
        started = int(step["trace_started"])
        self._trace(
            user,
            Span(
                trace_id=step["trace_id"],
                span_id=_step_span_id(step["step_id"]),
                name=OP_INVOKE_AGENT,
                kind=SpanKind.INTERNAL,
                start_unix_nano=started,
                status=SpanStatus.UNSET,
                attributes={
                    GEN_AI_OPERATION_NAME: OP_INVOKE_AGENT,
                    OATH_RUN_ID: step["run_id"],
                    OATH_STEP_ID: step["step_id"],
                    OATH_STEP_STATUS: step["status"],
                    OATH_STEP_TRIGGERS: list(step["trigger"]),
                    OATH_STEP_INPUT_COUNT: len(claimed),
                    OATH_STEP_LLM_CALLS: 0,
                },
                events=_input_events(started, claimed),
                links=links,
            ),
        )

    def _stage(self, user: User, step_id: str, name: str, attributes: JsonObject) -> None:
        """记一个 Brain 阶段节点。step span 要到最后才整条写出去，节点先攒着。

        攒在内存里而不是 journal 里：trace 不参与恢复，进程崩了这几个节点就该没有。
        同一个阶段重跑（失败重试）只留第一次，不重复记。
        """
        with self._lock:
            self._stages.setdefault(step_id, {})[name] = SpanEvent(name, time.time_ns(), attributes)

    def _take_stages(self, step_id: str) -> tuple[SpanEvent, ...]:
        with self._lock:
            return tuple(self._stages.pop(step_id, {}).values())

    def _close_step(self, user: User, step: State, status: str, llm_calls: int) -> None:
        """step 到终态时的第二条 span。同一个 span_id，读的时候后写的赢。

        输入事件在这里重读一遍，不靠认领时那份：后写的整条覆盖先写的，事件不补回来
        就等于从没写过。阶段节点与输入事件一起按时间排。
        """
        start = int(step["trace_started"])
        end = time.time_ns()
        state = self._store.read(user)
        entries = state["inputs"]
        inputs = [entries[key] for key in step["input_ids"] if key in entries]
        claimed = [(entry["queue"], entry["buffered_at"], entry["item"]) for entry in inputs]
        stages = self._take_stages(step["step_id"])
        self._trace(
            user,
            Span(
                trace_id=step["trace_id"],
                span_id=_step_span_id(step["step_id"]),
                name=OP_INVOKE_AGENT,
                kind=SpanKind.INTERNAL,
                start_unix_nano=start,
                end_unix_nano=end,
                status=status,
                attributes={
                    GEN_AI_OPERATION_NAME: OP_INVOKE_AGENT,
                    OATH_RUN_ID: step["run_id"],
                    OATH_STEP_ID: step["step_id"],
                    OATH_STEP_STATUS: step["status"],
                    OATH_STEP_TRIGGERS: list(step["trigger"]),
                    OATH_STEP_INPUT_COUNT: len(step["input_ids"]),
                    OATH_STEP_LLM_CALLS: llm_calls,
                    OATH_STEP_DURATION_MS: round((end - start) / 1_000_000, 3),
                },
                events=_ordered(_input_events(start, claimed) + stages),
                links=tuple(source_links(state, inputs)),
            ),
        )

    def _record_llm(
        self,
        user: User,
        step: State,
        attempt: int,
        started_ns: int,
        clock: float,
        first_delta: list[float],
        response: LlmResponse | None,
        error: str | None,
        first_content: list[float] | None = None,
    ) -> None:
        """一次模型调用。请求与响应原样进 span event，耗时进 attribute。"""
        end = time.monotonic()
        end_ns = time.time_ns()
        request = step["request"]
        duration_ms = (end - clock) * 1000
        attributes: JsonObject = {
            GEN_AI_OPERATION_NAME: OP_CHAT,
            GEN_AI_REQUEST_MODEL: request["model"],
            GEN_AI_REQUEST_MAX_TOKENS: request["max_output_tokens"],
            OATH_STEP_ID: step["step_id"],
            OATH_LLM_ATTEMPT: attempt,
            OATH_LLM_DURATION_MS: round(duration_ms, 3),
        }
        if request.get("temperature") is not None:
            attributes[GEN_AI_REQUEST_TEMPERATURE] = request["temperature"]
        ttft_ms = (first_delta[0] - clock) * 1000 if first_delta else None
        if ttft_ms is not None:
            attributes[OATH_LLM_TTFT_MS] = round(ttft_ms, 3)
        if first_content:
            attributes[OATH_LLM_FIRST_CONTENT_MS] = round((first_content[0] - clock) * 1000, 3)
        output_tokens = _output_tokens(response)
        if ttft_ms is not None and output_tokens is not None and output_tokens > 1:
            attributes[OATH_LLM_TPOT_MS] = round((duration_ms - ttft_ms) / (output_tokens - 1), 4)
        if response is not None:
            attributes[GEN_AI_RESPONSE_FINISH_REASONS] = [response.finish_reason]
            details = response.usage.get("prompt_tokens_details")
            cached = details.get("cached_tokens") if isinstance(details, dict) else None
            if cached is None:
                cached = response.usage.get("cache_read_input_tokens")
            if type(cached) is int and cached >= 0:
                attributes[GEN_AI_USAGE_CACHE_READ_TOKENS] = cached
            for key, name in (
                ("prompt_tokens", GEN_AI_USAGE_INPUT_TOKENS),
                ("completion_tokens", GEN_AI_USAGE_OUTPUT_TOKENS),
            ):
                value = response.usage.get(key)
                if isinstance(value, int):
                    attributes[name] = value
        events = [SpanEvent(REQUEST_EVENT, started_ns, {BODY: canonical_json(request)})]
        if response is not None:
            raw = canonical_json(_body(response))
            events.append(SpanEvent(RESPONSE_EVENT, end_ns, {BODY: raw}))
        self._trace(
            user,
            Span(
                trace_id=step["trace_id"],
                span_id=_llm_span_id(step["step_id"], attempt),
                parent_span_id=_step_span_id(step["step_id"]),
                name=chat_span_name(request["model"]),
                kind=SpanKind.CLIENT,
                start_unix_nano=started_ns,
                end_unix_nano=end_ns,
                status=SpanStatus.OK if error is None else SpanStatus.ERROR,
                attributes=attributes,
                events=tuple(events),
            ),
        )

    def _delta(self, user: User, step_id: str) -> Callable[[str, str], None]:
        counter = itertools.count(1)
        attempt = self._ctx.store.read(user)["steps"][step_id]["attempts"]

        def delta(channel: str, text: str) -> None:
            self.emit(
                user,
                MessageEventType.DELTA,
                {
                    "step_id": step_id,
                    "attempt": attempt,
                    "seq": next(counter),
                    "channel": channel,
                    "text": text,
                },
            )

        return delta

    def emit(self, user: User, type_: str, payload: State) -> None:
        self._ctx.sink.emit(
            MessageEvent(
                type=type_, user=user, payload=payload, idempotency_key="", occur_dt=utc_now_iso()
            )
        )

    def close(self, *, wait: bool = True) -> None:
        self._stop.set()
        with self._lock:
            self._stages.clear()


def _ordered(events: Sequence[SpanEvent]) -> tuple[SpanEvent, ...]:
    """span event 按时刻排。输入事件的时刻来自 buffer，可能早于 step 开始。"""
    return tuple(sorted(events, key=lambda event: event.unix_nano))


def _input_events(
    started: int, claimed: Sequence[tuple[str, float | None, State]]
) -> tuple[SpanEvent, ...]:
    """step 的每条输入各一个 span event，body 是那条 `Inbound` 的原始 JSON。

    时间用输入进 buffer 的时刻；没有就退回 step 开始的时间 —— 比统一填 0 诚实。
    """
    return tuple(
        SpanEvent(
            input_event_name(queue),
            int(at * 1_000_000_000) if at else started,
            {BODY: canonical_json(item)},
        )
        for queue, at, item in claimed
    )


def _step_span_id(step_id: str) -> str:
    """step 的 span_id 由 step_id 决定。

    认领和收尾各写一次，必须落到同一个 id 上（读的时候后写的赢）；失败重试走同一个
    step，同一个 id 也对得上。step_id 是 32 位 hex，取前 16 位正好是 OTel 的 span_id。
    """
    return step_id[:SPAN_ID_LENGTH]


def _llm_span_id(step_id: str, attempt: int) -> str:
    """一次模型调用一个 span。重试的每一次都留一条 —— 失败了几次是排查的第一现场。"""
    return f"{step_id[: SPAN_ID_LENGTH // 2]}{attempt:08x}"


def _output_tokens(response: LlmResponse | None) -> int | None:
    """输出 token 数。provider 没报就估算 —— tpot 的分母，宁可粗一点也不要没有。"""
    if response is None:
        return None
    usage = response.usage.get("completion_tokens")
    if isinstance(usage, int):
        return usage
    return estimate_text(response.content) + estimate_text(response.reasoning or "")


def _body(response: LlmResponse) -> JsonObject:
    """模型这一次原样说了什么。不经 `_response` 归一 —— 那是给对话历史用的。"""
    return {
        "content": response.content,
        "reasoning": response.reasoning,
        "finish_reason": response.finish_reason,
        "usage": dict(response.usage),
        "tool_calls": [
            {"id": call.tool_call_id, "name": call.tool, "arguments": call.arguments}
            for call in response.tool_calls
        ],
    }


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
