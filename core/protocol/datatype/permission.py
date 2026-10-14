"""权限系统专属的数据类型。

分两半：跨仓库的 wire 契约（`PermissionVerdict` / `PermissionEvent` /
`PermissionStatus` 等纯数据）与落盘/返回值（`PermissionRecord`）。前者跨仓库
消费，字段变更按破坏性变更处理。

权限判定是**三值**，不是布尔：`allow` 直接跑，`deny` 当场失败，`ask` 不跑、
转成一条待审批。这三个取值对应三条完全不同的后续路径，用一个 bool 表达不了 ——
`False` 到底是「拒绝」还是「要问人」，差别在调用方那边是致命的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, NotRequired, TypedDict

from core.protocol.datatype.event import AgentEvent
from core.protocol.datatype.scope import Scope
from core.utils.encoding import JsonObject, parse_timestamp, utc_now_iso


class PermissionVerdict:
    """策略对一次工具调用的判定。"""

    #: 直接跑。
    ALLOW: Final = "allow"
    #: 当场拒绝，进 `tool.result{outcome: "failed", error.kind: "permission_denied"}`。
    DENY: Final = "deny"
    #: 不执行，登记 waiting_approval；需要呈现时由 context 补 reminder，不发终态。
    ASK: Final = "ask"

    ALL: Final = frozenset({ALLOW, DENY, ASK})


class PermissionStatus:
    """一条审批请求的状态。"""

    #: 等人裁定。
    PENDING: Final = "pending"
    #: 同意了。
    APPROVED: Final = "approved"
    #: 拒绝了。
    DENIED: Final = "denied"
    #: 有效期到了没人管。
    EXPIRED: Final = "expired"

    #: 已终结。到这里之后任何裁定都不再生效。
    RESOLVED: Final = frozenset({APPROVED, DENIED, EXPIRED})
    ALL: Final = frozenset({PENDING}) | RESOLVED

    @classmethod
    def is_resolved(cls, status: str) -> bool:
        return status in cls.RESOLVED


class PermissionEventType:
    """权限系统发出的事件类型。

    requested 用于展示授权卡；执行状态通过查询读取，真实终态进入 tool queue。
    resolved 是可用的展示事件类型，本执行器不以它唤醒模型。
    """

    REQUESTED: Final = "permission.requested"
    RESOLVED: Final = "permission.resolved"

    ALL: Final = frozenset({REQUESTED, RESOLVED})


#: 幂等键前缀。刻意取短：键长上限是 journal 定的，而 `invocation_id` 的长度上限
#: 是从「最长的那个前缀」反推的，前缀越短，能接受的 `invocation_id` 越长。
#: `perm.req:` / `perm.res:` 是 9 字符，比 `tool.result:` 还短，不构成约束。
PERMISSION_REQUESTED_KEY_PREFIX: Final = "perm.req:"
PERMISSION_RESOLVED_KEY_PREFIX: Final = "perm.res:"


class PermissionRecordType:
    """存储里的一行。请求与裁定各一行，同一个 `invocation_id` 后者覆盖前者。"""

    REQUESTED: Final = "requested"
    RESOLVED: Final = "resolved"

    ALL: Final = frozenset({REQUESTED, RESOLVED})


class PermissionRecordDict(TypedDict):
    """落盘的一行。字段名与 `PermissionRecord` 的字段一一对应。"""

    scope_user_id: str
    scope_session_id: str
    type: str
    tool_call_id: str
    invocation_id: str
    tool: str
    arguments: JsonObject
    status: str
    requested_dt: str
    expires_dt: str
    decided_by: NotRequired[str | None]
    decided_dt: NotRequired[str | None]
    reason: NotRequired[str | None]


# --------------------------------------------------------------------------
# 跨仓库的 wire 契约
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PermissionEvent(AgentEvent):
    """审批请求，或它的裁定结果。

    `payload` 逐字段对齐落盘记录的 payload，所以投递到存储时可以直接 append，
    不需要翻译。字段变更按破坏性变更处理。
    """

    #: 审批状态，取值见 `PermissionStatus`。
    status: str

    def to_dict(self) -> JsonObject:
        return {
            "type": self.type,
            "user_id": self.scope.user_id,
            "session_id": self.scope.session_id,
            "tool_call_id": self.tool_call_id,
            "invocation_id": self.invocation_id,
            "tool": self.tool,
            "status": self.status,
            "payload": self.payload,
            "idempotency_key": self.idempotency_key,
            "occur_dt": self.occur_dt,
        }


# --------------------------------------------------------------------------
# 存储与返回值
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PermissionRecord:
    """一条审批请求的完整状态。

    `arguments` 存的是**判权限时看到的那些入参** —— 批注的人要看的就是它们，
    而它们同时是这道裁定的适用范围：同一个 `invocation_id` 拿着另一组入参回来，
    不回放这道裁定。批的是某一次具体调用，不是那个 id。

    时间戳只用于展示与判定过期，不参与排序（对照 journal：水位线用游标不用时间戳，
    墙钟会回拨）。
    """

    scope: Scope
    tool_call_id: str
    invocation_id: str
    tool: str
    arguments: JsonObject
    status: str
    requested_dt: str
    expires_dt: str
    decided_by: str | None = None
    decided_dt: str | None = None
    reason: str | None = None

    @property
    def is_resolved(self) -> bool:
        return PermissionStatus.is_resolved(self.status)

    def to_dict(self) -> PermissionRecordDict:
        return PermissionRecordDict(
            scope_user_id=self.scope.user_id,
            scope_session_id=self.scope.session_id,
            type=(
                PermissionRecordType.RESOLVED
                if self.is_resolved
                else PermissionRecordType.REQUESTED
            ),
            tool_call_id=self.tool_call_id,
            invocation_id=self.invocation_id,
            tool=self.tool,
            arguments=self.arguments,
            status=self.status,
            requested_dt=self.requested_dt,
            expires_dt=self.expires_dt,
            decided_by=self.decided_by,
            decided_dt=self.decided_dt,
            reason=self.reason,
        )

    def payload(self) -> JsonObject:
        """事件 payload。与落盘记录的 payload 同形 —— 两者本来就该是同一份。"""
        payload: JsonObject = {
            "tool_call_id": self.tool_call_id,
            "invocation_id": self.invocation_id,
            "tool": self.tool,
            "arguments": self.arguments,
            "status": self.status,
            "requested_dt": self.requested_dt,
            "expires_dt": self.expires_dt,
        }
        if self.decided_by is not None:
            payload["decided_by"] = self.decided_by
        if self.decided_dt is not None:
            payload["decided_dt"] = self.decided_dt
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


def expired(record: PermissionRecord) -> bool:
    """这条请求的有效期是否已过。

    墙钟会回拨，回拨会让「已过期」变回「没过期」，所以这里只当**展示**用 ——
    真正把一条请求判成过期的是 watchdog 定时器，不依赖任何一次时间比较。
    """
    return parse_timestamp(record.expires_dt) <= parse_timestamp(utc_now_iso())


__all__ = [
    "PERMISSION_REQUESTED_KEY_PREFIX",
    "PERMISSION_RESOLVED_KEY_PREFIX",
    "PermissionEvent",
    "PermissionEventType",
    "PermissionRecord",
    "PermissionRecordDict",
    "PermissionRecordType",
    "PermissionStatus",
    "PermissionVerdict",
    "expired",
]
