"""工具系统专属的数据类型。

分两半：跨仓库的 wire 契约（`ToolDefinition` / `ToolEvent` / `ToolStatus` 等纯数据）
与接口返回值（`ToolExecution` / `BatchExecution`）。前者跨仓库消费，字段变更按
破坏性变更处理；后者可以在不破坏契约的前提下变。

一次工具调用牵两个标识，来源不同，不能合并：

| 标识 | 谁生成 | 干什么 |
| --- | --- | --- |
| `tool_call_id` | **模型 API** | 组对话历史时对齐 assistant 的 `tool_calls` 与 tool 消息 |
| `invocation_id` | Oath | 状态查询的键，也是 `tool.call` ↔ `tool.result` 的配对键 |

`tool_call_id` 原样透传：assistant 轮里的 `tool_calls[i].id` 和 tool 结果消息里的
`tool_call_id` 必须逐字节相同，任何 provider 都会拿这对值做配对，对不上就拒收整条
请求。Oath 不生成它，也不解释它。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from core.protocol.datatype.event import AgentEvent
from core.protocol.datatype.journal import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
    ToolOutcome,
)
from core.protocol.datatype.scope import Scope
from core.utils.encoding import JsonObject, JsonValue

#: 工具名的合法字符集。多家 provider 拒收含点、连字符、空格或大写的工具名 ——
#: 函数调用协议把名字直接拼进请求体。这是跨 provider 的公共约束，不是某一家的口味。
TOOL_NAME_PATTERN: Final = r"^[a-z][a-z0-9_]{0,63}$"

_TOOL_NAME_RE: Final = re.compile(TOOL_NAME_PATTERN)

#: `tool.result` 幂等键的前缀。
RESULT_KEY_PREFIX: Final = "tool.result:"

#: 调用标识的长度上限。它要拼进幂等键，所以上限从 journal 的键长上限反推，
#: 而不是另定一个数。
MAX_INVOCATION_ID_LENGTH: Final = MAX_IDEMPOTENCY_KEY_LENGTH - len(RESULT_KEY_PREFIX)


def is_valid_tool_name(value: str) -> bool:
    """判断工具名是否符合各 provider 共同接受的字符集。"""
    return _TOOL_NAME_RE.fullmatch(value) is not None


def is_valid_invocation_id(value: str) -> bool:
    """判断调用标识能否用作幂等键的一段。

    **只管长度，不管字符集。** `invocation_id` 由 Oath 生成，形态是实现细节；
    真正硬约束只有一条 —— 拼上 `RESULT_KEY_PREFIX` 之后要落在 journal 的
    键长度上限内。
    """
    return 0 < len(value) <= MAX_INVOCATION_ID_LENGTH


class ToolStatus:
    """一次调用的持久化状态。

    终态的取值与 ToolOutcome 一致；admitting / waiting_approval / queued / running
    都是中间状态，没有 outcome。
    """

    ADMITTING: Final = "admitting"
    CANCELLED: Final = ToolOutcome.CANCELLED
    #: 已接纳，线程还没拿到。
    QUEUED: Final = "queued"
    #: 工具正在执行。
    RUNNING: Final = "running"
    #: 工具正常返回。
    SUCCEEDED: Final = ToolOutcome.SUCCEEDED
    #: 被拒、被取消权限，或工具抛了异常。
    FAILED: Final = ToolOutcome.FAILED
    #: core 到 deadline 就不再等待。工具线程**可能还在跑**。
    TIMEOUT: Final = ToolOutcome.TIMEOUT
    #: 工具可能执行过，但没有可靠结果；通知丢失不构成 unknown。
    OUTCOME_UNKNOWN: Final = ToolOutcome.OUTCOME_UNKNOWN
    #: 工具**没执行**：权限判定是 ask，在等人批。没有东西出错，所以不是失败。
    WAITING_APPROVAL: Final = "waiting_approval"

    #: 终态 → outcome 的映射。非终态不在表里。
    OUTCOMES: Final[dict[str, str]] = {
        SUCCEEDED: ToolOutcome.SUCCEEDED,
        FAILED: ToolOutcome.FAILED,
        TIMEOUT: ToolOutcome.TIMEOUT,
        OUTCOME_UNKNOWN: ToolOutcome.OUTCOME_UNKNOWN,
        CANCELLED: ToolOutcome.CANCELLED,
    }

    #: 全部终态。到达其中之一后，这条执行的结果不可再被改写。
    TERMINAL: Final = frozenset(OUTCOMES)

    @classmethod
    def is_terminal(cls, status: str) -> bool:
        return status in cls.TERMINAL


class ToolErrorKind:
    """失败的分类。

    `message` 是给人看的，`kind` 是给机器看的：上层据此决定要不要重试、要不要
    提示模型改参数。字符串本身不该被拿去比较。
    """

    #: 工具名不在注册表里。
    UNKNOWN_TOOL: Final = "unknown_tool"
    #: 权限检查拒绝。
    PERMISSION_DENIED: Final = "permission_denied"
    #: 权限这一步**没能完成**（存储故障、策略返回了没约定的判定）。与
    #: `permission_denied` 分开：那是「判定为拒绝」，这是「问不出结果」。
    PERMISSION_ERROR: Final = "permission_error"
    #: 入参不符合 `input_schema`。
    INVALID_ARGUMENTS: Final = "invalid_arguments"
    #: 执行中与排队中已达上限，工具系统不阻塞，立即拒。
    BUSY: Final = "busy"
    #: 工具自己抛了 `ToolExecutionError`。
    TOOL_ERROR: Final = "tool_error"
    #: 工具抛了别的异常。
    UNEXPECTED: Final = "unexpected"
    #: core 到 deadline 不再等待。
    TIMEOUT: Final = "timeout"
    #: 工具跑过了，但终态事件投递失败。
    SINK_ERROR: Final = "sink_error"

    ALL: Final = frozenset(
        {
            UNKNOWN_TOOL,
            PERMISSION_DENIED,
            PERMISSION_ERROR,
            INVALID_ARGUMENTS,
            BUSY,
            TOOL_ERROR,
            UNEXPECTED,
            TIMEOUT,
            SINK_ERROR,
        }
    )


class ToolEventType:
    """工具系统发出的事件类型。

    v1 只有一个：终态。开始执行不发 —— agent 只关心「这次调用结束了没有」，
    中间态留在执行表里给 `status()` 查，不进事件流。
    """

    TERMINAL: Final = "tool.terminal"

    ALL: Final = frozenset({TERMINAL})


class ToolHandleState:
    """`execute` 对某个项的当场结论。"""

    #: 已排队，等线程。
    ACCEPTED: Final = "accepted"
    #: 同一个调用已经在执行中，返回同一次执行，不重复执行外部副作用。
    DEDUPLICATED: Final = "deduplicated"
    ALL: Final = frozenset({ACCEPTED, DEDUPLICATED})


# --------------------------------------------------------------------------
# 跨仓库的 wire 契约
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """一个工具的 provider 无关定义。

    字段名用通用名，不带任何一家的 provider 字段名（对照 `Message` 的
    `role` / `content`）。转成各家请求格式是模型适配层的事。

    `input_schema` 同时是给模型看的参数说明和服务端入参校验的依据 —— 两处
    共用一份，才不会「模型看到的结构」和「校验的结构」分家。
    """

    name: str
    description: str
    input_schema: JsonObject

    def to_dict(self) -> JsonObject:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass(frozen=True, slots=True)
class ToolEvent(AgentEvent):
    """工具执行的终态事件。

    `payload` 逐字段对齐 `PAYLOAD_SCHEMAS[(EventType.TOOL_RESULT, 1)]`，所以
    投递到 journal 时可以直接 append，不需要翻译。字段变更按破坏性变更处理。
    """

    #: 执行的终态，取值见 `ToolOutcome`。
    outcome: str

    def to_dict(self) -> JsonObject:
        return {
            "type": self.type,
            "user_id": self.scope.user_id,
            "session_id": self.scope.session_id,
            "tool_call_id": self.tool_call_id,
            "invocation_id": self.invocation_id,
            "tool": self.tool,
            "outcome": self.outcome,
            "payload": self.payload,
            "idempotency_key": self.idempotency_key,
            "occur_dt": self.occur_dt,
        }


# --------------------------------------------------------------------------
# 接口返回值
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """一次待派发的调用。

    `tool_call_id` 必填：它来自模型 API 的响应，终态事件要原样带回去，
    组对话历史时靠它和 assistant 轮里的 `tool_calls` 对齐。没有模型调用就没有
    工具调用，这个字段不能省。

    `invocation_id` 省略时由工具系统生成 —— 状态查询用它，不是调用方的义务。
    """

    tool: str
    arguments: JsonObject
    tool_call_id: str
    invocation_id: str | None = None
    batch_id: str = ""
    run_id: str = ""


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """交给工具的入参。

    `deadline` 是 monotonic 秒，工具可以拿它给自己要发的 I/O 设更短的内部
    超时。它只是提示：core 到点就不再等待，不靠它收手。
    """

    scope: Scope
    invocation_id: str
    arguments: JsonObject
    deadline: float | None


@dataclass(frozen=True, slots=True)
class ToolResult:
    """工具的返回值。

    只有 `content` 一个字段。journal 的 `tool.result` payload 除了两个 id 与
    outcome 之外只允许 `content` 和 `error` —— 扩展自己往结果里塞元数据会破掉
    「事件 payload 恰好是合法 `tool.result` payload」这条不变量。

    失败不靠返回空结果表示，抛 `ToolExecutionError`。返回值就等于成功。
    """

    content: JsonValue = None


@dataclass(frozen=True, slots=True)
class ToolErrorInfo:
    """失败的原因。刻意不带 traceback。"""

    kind: str
    message: str

    def to_payload(self) -> JsonObject:
        return {"kind": self.kind, "message": self.message}


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """执行状态查询的结果。

    时间戳只用于展示与排查，**不参与任何判定** —— 墙钟会回拨，做水位线会静默
    丢数据（对照 journal 的 `cursor` 不存时间戳）。
    """

    scope: Scope
    invocation_id: str
    tool_call_id: str
    tool: str
    arguments: JsonObject
    status: str
    result: ToolResult | None
    error: ToolErrorInfo | None
    submitted_dt: str
    started_dt: str | None = None
    finished_dt: str | None = None
    batch_id: str = ""
    source: str = "brain"
    event_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        return ToolStatus.is_terminal(self.status)

    @property
    def outcome(self) -> str | None:
        """终态对应的 outcome；未终结时为 `None`。"""
        return ToolStatus.OUTCOMES.get(self.status)


@dataclass(frozen=True, slots=True)
class ToolHandle:
    """`execute` 对某个项的当场结论。

    只回答「谁被接下了」，不回答结果 —— 结果走事件。
    """

    invocation_id: str
    tool: str
    state: str
    error: ToolErrorInfo | None = None


@dataclass(frozen=True, slots=True)
class BatchExecution:
    """一次批量派发的结果，与请求逐项对应。"""

    handles: tuple[ToolHandle, ...] = field(default_factory=tuple)


__all__ = [
    "MAX_INVOCATION_ID_LENGTH",
    "RESULT_KEY_PREFIX",
    "TOOL_NAME_PATTERN",
    "BatchExecution",
    "ToolCallRequest",
    "ToolDefinition",
    "ToolErrorInfo",
    "ToolErrorKind",
    "ToolEvent",
    "ToolEventType",
    "ToolExecution",
    "ToolHandle",
    "ToolHandleState",
    "ToolInvocation",
    "ToolResult",
    "ToolStatus",
    "is_valid_invocation_id",
    "is_valid_tool_name",
]
