"""journal 专属的数据类型。

分两半：落盘的记录格式（`EventRecord` / `StateRecord` / `EventType` /
`ToolOutcome` / payload schema / 行编码常量）与接口返回值（`Record` /
`AppendResult` / `StateRef` / `Recovery`）。前者是跨仓库协议，字段变更按破坏性
变更处理；后者可以在不破坏契约的前提下变。

`Message` 和 `AgentState` 不在这里，它们不是 journal 专属的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, NotRequired, TypedDict

from core.protocol.datatype.agent_state import AgentState
from core.protocol.datatype.cursor import Cursor
from core.protocol.datatype.message import Message
from core.utils.encoding import JsonObject, JsonValue

# --------------------------------------------------------------------------
# 落盘的记录格式
# --------------------------------------------------------------------------


class EventType:
    """事件日志的记录类型。

    v1 的闭合集合，加新成员按破坏性变更处理。`system.` 只是个命名约定，不是
    特殊类别 —— 和 `user.message` 一样是一种 type，一样的登记流程，一样的字段约束。
    """

    USER_MESSAGE: Final = "user.message"
    ASSISTANT_MESSAGE: Final = "assistant.message"
    TOOL_CALL: Final = "tool.call"
    TOOL_RESULT: Final = "tool.result"
    SYSTEM_AGENT_STEP: Final = "system.agent_step"
    #: 终止当前 run。进了 system 队列：runtime 该停下并收尾。
    SYSTEM_CANCEL: Final = "system.cancel"
    #: 往当前 run 里插一条 steer，run 不终止。上下文里显式打断用。
    SYSTEM_INTERRUPT: Final = "system.interrupt"

    #: 全部合法的 type。
    ALL: Final = frozenset(
        {
            USER_MESSAGE,
            ASSISTANT_MESSAGE,
            TOOL_CALL,
            TOOL_RESULT,
            SYSTEM_AGENT_STEP,
            SYSTEM_CANCEL,
            SYSTEM_INTERRUPT,
        }
    )

    #: runtime 要处理的控制面指令。`system.agent_step` 不在里面 —— 那是 step
    #: 生命周期的审计记录，消费方是 trace 视图，不是 runtime。
    SYSTEM_COMMANDS: Final = frozenset({SYSTEM_CANCEL, SYSTEM_INTERRUPT})

    #: 参与 messages 投影的子集。其余只进日志，不进对话历史。
    MESSAGE_TYPES: Final = frozenset({USER_MESSAGE, ASSISTANT_MESSAGE, TOOL_CALL, TOOL_RESULT})


class ToolOutcome:
    """`tool.result` 的终态取值。

    `OUTCOME_UNKNOWN` 是刻意存在的一支：工具执行过但结果丢了（网络中断、
    进程被杀、对端超时无响应）。这种情况**不能**一律重试 —— 幂等键不保证
    第三方系统去重。journal 只存不判，重试策略属于工具系统。

    `AWAITING_APPROVAL` 也刻意存在：工具**没执行**，因为权限判定要人批。这不是
    失败 —— 没有东西出错。而它必须是个终态：LLM 的 message 数组里每个
    tool call 都要配一条 tool result，缺一条这个数组交给任何 provider 都是坏的。
    """

    SUCCEEDED: Final = "succeeded"
    FAILED: Final = "failed"
    TIMEOUT: Final = "timeout"
    CANCELLED: Final = "cancelled"
    OUTCOME_UNKNOWN: Final = "outcome_unknown"
    AWAITING_APPROVAL: Final = "awaiting_approval"

    ALL: Final = frozenset(
        {SUCCEEDED, FAILED, TIMEOUT, CANCELLED, OUTCOME_UNKNOWN, AWAITING_APPROVAL}
    )


class EventRecord(TypedDict):
    """事件日志一行。`id` 只有 MySQL 实现会写，JSONL 实现省略。"""

    user_id: str
    session_id: str
    id: NotRequired[int]
    type: str
    payload: JsonObject
    payload_schema_version: int
    idempotency_key: str
    occur_dt: str | None
    commit_dt: str


class StateRecord(TypedDict):
    """状态快照一行。全量 KV，不是 delta。"""

    user_id: str
    session_id: str
    id: NotRequired[int]
    commit_dt: str
    payload_schema_version: int
    payload: JsonObject
    cursor_start: JsonValue
    cursor_end: JsonObject


#: 幂等键长度上限。键来自调用方（多为 `invocation_id` 之类的稳定标识），
#: 不是自由文本，不设上限的话一个超长键能把索引撑爆。
MAX_IDEMPOTENCY_KEY_LENGTH: Final = 128


class RecordFormat:
    """行编码常量。写进日志的每一行都以 `\\n` 结尾。"""

    LINE_TERMINATOR: Final = "\n"
    FILE_MODE: Final = 0o600

    EVENTS_FILENAME: Final = "events.jsonl"
    STATE_FILENAME: Final = "state.jsonl"


@dataclass(frozen=True, slots=True)
class PayloadSchema:
    """某个 `type` 在某个版本下 payload 的字段约束。

    校验是**结构**校验（必填字段在不在、有没有未知字段），不做业务语义校验。
    `nullable` 里的字段必须出现但可以是 `null` —— 与「字段缺失」是两件事，
    跨版本读取时行为不同，所以显式区分。
    """

    required: frozenset[str] = frozenset()
    nullable: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()

    def allowed(self) -> frozenset[str]:
        return self.required | self.nullable | self.optional


#: v1 的 payload schema。键是 `(type, version)`，版本从 1 开始。
#: 每种 type 都有 schema，没有例外。
PAYLOAD_SCHEMAS: Final[dict[tuple[str, int], PayloadSchema]] = {
    (EventType.USER_MESSAGE, 1): PayloadSchema(required=frozenset({"content"})),
    # `reasoning` 必填但可为 null：模型的思维链原样落盘，不加工。
    # 是否在重放时回传给模型由 provider 策略决定，那是适配层的事。
    (EventType.ASSISTANT_MESSAGE, 1): PayloadSchema(
        required=frozenset({"content", "reasoning"}),
        optional=frozenset({"tool_calls", "model", "usage"}),
    ),
    # `tool_call_id` 与 `invocation_id` 是两个不同的标识，不是一对别名：
    # 前者由模型 API 返回、必须原样透传进对话历史，后者是 Oath 自己的调用 id。
    # 分开存，投影才有办法在多次结果之间挑出终态，同时仍把模型认的那个 id 原样还回去。
    (EventType.TOOL_CALL, 1): PayloadSchema(
        required=frozenset({"tool_call_id", "invocation_id", "tool", "arguments"})
    ),
    (EventType.TOOL_RESULT, 1): PayloadSchema(
        required=frozenset({"tool_call_id", "invocation_id", "outcome"}),
        optional=frozenset({"content", "error"}),
    ),
    (EventType.SYSTEM_AGENT_STEP, 1): PayloadSchema(required=frozenset({"step_id", "phase"})),
    (EventType.SYSTEM_CANCEL, 1): PayloadSchema(
        required=frozenset({"run_id"}), optional=frozenset({"reason"})
    ),
    (EventType.SYSTEM_INTERRUPT, 1): PayloadSchema(
        required=frozenset({"run_id"}), optional=frozenset({"note"})
    ),
}

#: 状态快照的 payload 是一个 KV 对象，形状由 agent runtime 决定，
#: journal 只要求它是 JSON 对象。版本从 1 开始。
STATE_PAYLOAD_SCHEMAS: Final[dict[int, PayloadSchema]] = {1: PayloadSchema()}


# --------------------------------------------------------------------------
# 接口返回值
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Record:
    """一条已落盘的事件记录，附带它在日志里的位置。"""

    cursor: Cursor
    type: str
    payload: JsonObject
    payload_schema_version: int
    idempotency_key: str
    occur_dt: str | None
    commit_dt: str
    raw: EventRecord

    @classmethod
    def of(cls, cursor: Cursor, raw: EventRecord) -> Record:
        return cls(
            cursor=cursor,
            type=raw["type"],
            payload=raw["payload"],
            payload_schema_version=raw["payload_schema_version"],
            idempotency_key=raw["idempotency_key"],
            occur_dt=raw["occur_dt"],
            commit_dt=raw["commit_dt"],
            raw=raw,
        )


@dataclass(frozen=True, slots=True)
class AppendResult:
    """`append` 的结果。

    `idempotent_hit=True` 表示这次调用命中了已有的幂等键、没有追加新行。
    `record_id` 只有会写自增 id 的实现（MySQL）才非空；JSONL 实现用游标定位。
    """

    cursor: Cursor
    record_id: int | None
    idempotent_hit: bool


@dataclass(frozen=True, slots=True)
class StateRef:
    """`commit_state` 的结果。

    `covered_through` 是这份快照覆盖到的事件日志位置（= `state.cursor_end`），
    不是状态日志里的行号 —— 调用方要的是「从这儿往后才是新的」。
    """

    covered_through: Cursor
    commit_dt: str


@dataclass(frozen=True, slots=True)
class Recovery:
    """一次恢复的结果。

    `state.cursor_end` 与 `messages` 严格对齐：把截至快照的 state 和截至当前的
    messages 配在一起交给模型，会出现「模型记得这次工具调用，但 state 里没有它
    生效」的错位。水位线只存在于 `state` 上，不重复一份。

    `pending` 是 `cursor_end` 之后的全部记录。journal **不**折叠它们 —— fold 需要
    知道「工具结果如何改变 state」，那是 agent runtime 的业务语义。调用方的
    恢复流程是两段：

        journal.recover(scope)                    →  自洽的 (state, messages) + pending
        runtime: fold(state, pending)             →  最新 state（纯函数，无副作用）
        journal.commit_state(new_state)               →  落盘，下次 recover 更快
    """

    state: AgentState
    messages: list[Message]
    pending: list[Record]


__all__ = [
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "PAYLOAD_SCHEMAS",
    "STATE_PAYLOAD_SCHEMAS",
    "AppendResult",
    "EventRecord",
    "EventType",
    "PayloadSchema",
    "Record",
    "RecordFormat",
    "Recovery",
    "StateRecord",
    "StateRef",
    "ToolOutcome",
]
