"""span 的词汇表：属性名、事件名、span 名。

分两段：`GEN_AI` 是 OpenTelemetry GenAI 语义约定里已有的，不改；`OATH` 是我们自己
补的 —— OTel 没定的部分才自己起名。

这一份只是名字。谁在什么时刻知道某个值为真，就由谁去填（现在是 Brain）。
"""

from __future__ import annotations

from typing import Final

#: 操作名。GenAI 约定把一次 agent 推理叫 invoke_agent，一次模型调用叫 chat。
OP_INVOKE_AGENT: Final = "invoke_agent"
OP_CHAT: Final = "chat"

# -- 属性 ---------------------------------------------------------------------

GEN_AI_OPERATION_NAME: Final = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL: Final = "gen_ai.request.model"
GEN_AI_REQUEST_TEMPERATURE: Final = "gen_ai.request.temperature"
GEN_AI_REQUEST_MAX_TOKENS: Final = "gen_ai.request.max_tokens"
GEN_AI_USAGE_INPUT_TOKENS: Final = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS: Final = "gen_ai.usage.output_tokens"
GEN_AI_RESPONSE_FINISH_REASONS: Final = "gen_ai.response.finish_reasons"

OATH_STEP_ID: Final = "oath.step.id"
OATH_STEP_STATUS: Final = "oath.step.status"
OATH_STEP_TRIGGERS: Final = "oath.step.triggers"
OATH_STEP_INPUT_COUNT: Final = "oath.step.input_count"
OATH_STEP_LLM_CALLS: Final = "oath.step.llm_calls"
OATH_STEP_TOOL_CALLS: Final = "oath.step.tool_calls"
OATH_STEP_DURATION_MS: Final = "oath.step.duration_ms"
OATH_RUN_ID: Final = "oath.run.id"

OATH_LLM_ATTEMPT: Final = "oath.llm.attempt"
OATH_LLM_TTFT_MS: Final = "oath.llm.ttft_ms"
OATH_LLM_TPOT_MS: Final = "oath.llm.tpot_ms"
OATH_LLM_DURATION_MS: Final = "oath.llm.duration_ms"

# -- 事件 ---------------------------------------------------------------------

#: step 的每条输入各一个，名字带队列名。
INPUT_EVENT: Final = "oath.input.{queue}"

#: 一次模型调用的原始输入与输出。放 event 不放 attribute：attribute 该是小标量，
#: 一整份报文是「那一刻发生的事」。
REQUEST_EVENT: Final = "gen_ai.request"
RESPONSE_EVENT: Final = "gen_ai.response"
BODY: Final = "body"

#: Brain 的固定阶段节点，全是 step span 上的 event，带时刻。
#:
#: 只有一个 step 总耗时是不够的：模型调用有自己的 span，剩下的时间（认领、压缩判定、
#: 请求冻结、工具批次提交）全挤在 step span 里。慢的时候要能一眼看出慢在哪一段。
STAGE_REQUEST_PREPARED: Final = "oath.stage.request_prepared"
STAGE_TOOLS_SUBMITTED: Final = "oath.stage.tools_submitted"
STAGE_COMPLETED: Final = "oath.stage.completed"


def chat_span_name(model: str) -> str:
    return f"{OP_CHAT} {model}" if model else OP_CHAT


def input_event_name(queue: str) -> str:
    return INPUT_EVENT.format(queue=queue)


__all__ = [
    "BODY",
    "GEN_AI_OPERATION_NAME",
    "GEN_AI_REQUEST_MAX_TOKENS",
    "GEN_AI_REQUEST_MODEL",
    "GEN_AI_REQUEST_TEMPERATURE",
    "GEN_AI_RESPONSE_FINISH_REASONS",
    "GEN_AI_USAGE_INPUT_TOKENS",
    "GEN_AI_USAGE_OUTPUT_TOKENS",
    "INPUT_EVENT",
    "OP_CHAT",
    "OP_INVOKE_AGENT",
    "OATH_LLM_ATTEMPT",
    "OATH_LLM_DURATION_MS",
    "OATH_LLM_TPOT_MS",
    "OATH_LLM_TTFT_MS",
    "OATH_RUN_ID",
    "OATH_STEP_DURATION_MS",
    "OATH_STEP_ID",
    "OATH_STEP_INPUT_COUNT",
    "OATH_STEP_LLM_CALLS",
    "OATH_STEP_TOOL_CALLS",
    "OATH_STEP_STATUS",
    "OATH_STEP_TRIGGERS",
    "REQUEST_EVENT",
    "RESPONSE_EVENT",
    "STAGE_COMPLETED",
    "STAGE_REQUEST_PREPARED",
    "STAGE_TOOLS_SUBMITTED",
    "chat_span_name",
    "input_event_name",
]
