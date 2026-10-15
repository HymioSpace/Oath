"""Oath 对外的帧格式 —— 一条双向连接上的 command 与 event。

一条 WebSocket 同时跑两个方向：客户端发 command、收 event。拆成两条只会多一次
握手和一份鉴权，而这里两个方向绑的是同一个 session。

帧是统一的 envelope：`v` 是协议版本，`type` 是这一帧是什么。**版本不认识就断开重协商，
不猜** —— 猜出来的结果是「按旧版解析成功、按新版执行失败」，失败点会落在某个看起来无关的
地方。

上行 `type` 取 `CommandType`（command 自己的闭合集合），下行取 `FrameType`（event /
pong / error / close）。加一个 command 是破坏性变更：老客户端遇到
不认识的 type 必须当协议错误断开，而不是跳过 —— command 有副作用，跳过等于
让两端的认知分叉。

用户身份**不在上行帧里**。`user_id` 由服务端从 `credential` 推导，客户端不能自称：
隔离键一旦能被调用方伪造，越权读就不是漏洞而是默认行为（同一理由见 journal 的
`ScopeError`）。

下行是**活的投递**，不是 journal 的转发：`event` 帧不带游标，断线期间发生的事件不补发。
客户端自己就是它已渲染内容的存储方，让 oath 拿 journal 当消息总线只会把投递能力绑死在
日志扫描上。

ACK 表示 session 绑定成功或命令已可靠接纳；不表示工具执行完成。
断线后用 session.status 查询审批、调用和 step 状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from core.protocol.datatype.event import AgentEvent
from core.protocol.datatype.journal import MAX_IDEMPOTENCY_KEY_LENGTH, PayloadSchema
from core.protocol.error import WireProtocolError
from core.utils.encoding import JsonObject, JsonValue

#: command id 的长度上限。它要挂进 `user.message` 的幂等键，所以上限从 journal 的键长
#: 上限反推。**在这里就挡住**：一条落不了盘的输入进了队列，就只能卡着 session 重试
#: 到天荒地老 —— 后来的输入全被它拖着。
MAX_COMMAND_ID_LENGTH: Final = MAX_IDEMPOTENCY_KEY_LENGTH - len("cmd:")


class WireVersion:
    """协议版本。信封里的 `v` 就是它，不匹配即断开。"""

    CURRENT: Final = 1


class FrameType:
    """下行帧的种类。信封里的 `type`。

    只有下行有这几个值：上行的 `type` 是 command 自己的类型（`CommandType`）。
    两套命名空间不重叠，一个字段就能表达「这是什么」，不需要先判方向再判种类。
    """

    #: oath → 客户端。一条事件。
    EVENT: Final = "event"
    #: oath → 客户端。应用层回执。
    ACK: Final = "ack"
    #: oath → 客户端。`ping` 的回声。
    PONG: Final = "pong"
    #: oath → 客户端。command 收到了但没办成，或帧本身不合法。
    ERROR: Final = "error"
    #: oath → 客户端。主动断开，附理由。
    CLOSE: Final = "close"


class CommandType:
    """客户端能发的 command。闭合集合。"""

    #: 绑定 session。`user_id` 由此处的 credential 决定。
    SESSION_OPEN: Final = "session.open"
    #: 用户输入。会落成 `user.message`。
    USER_MESSAGE: Final = "user.message"
    #: 审批决定先可靠接纳，再经 system consumer 原子裁定。
    PERMISSION_DECIDE: Final = "permission.decide"
    #: 取消指定或当前 run；控制动作先于可能的模型解释。
    RUN_CANCEL: Final = "run.cancel"
    #: 显式重试暂停 / 失败的原 step。
    STEP_RETRY: Final = "step.retry"
    SESSION_STATUS: Final = "session.status"
    #: 当前 session 有哪些 trace。
    TRACE_LIST: Final = "trace.list"
    #: 取一条 trace 的全部 span。
    TRACE_GET: Final = "trace.get"
    PING: Final = "ping"

    ALL: Final = frozenset(
        {
            SESSION_OPEN,
            USER_MESSAGE,
            PERMISSION_DECIDE,
            RUN_CANCEL,
            PING,
            STEP_RETRY,
            SESSION_STATUS,
            TRACE_LIST,
            TRACE_GET,
        }
    )

    #: 查询与心跳：不产生输入，也不改任何状态。
    QUERY: Final = frozenset({PING, SESSION_STATUS, TRACE_LIST, TRACE_GET})

    #: 不需要 `session_id` 的 command —— 绑定 session 之前还没有 session_id。
    ANONYMOUS: Final = frozenset({SESSION_OPEN})


class CloseReason:
    """服务端主动断开时带的理由。客户端据此决定退避多久。"""

    #: 发送队列满了。这条连接读得太慢，见「背压」。
    SLOW_CONSUMER: Final = "slow_consumer"
    #: credential 无效或不认。不回落到匿名。
    UNAUTHORIZED: Final = "unauthorized"
    #: 同一 session 又有了一条连接，前一条让位。
    SESSION_REPLACED: Final = "session_replaced"
    #: 帧不合法：版本、字段、payload 三样之一不对。
    PROTOCOL_ERROR: Final = "protocol_error"
    #: 空闲超时。没有任何 command 的连接不会一直占着。
    IDLE_TIMEOUT: Final = "idle_timeout"

    ALL: Final = frozenset(
        {
            SLOW_CONSUMER,
            UNAUTHORIZED,
            SESSION_REPLACED,
            PROTOCOL_ERROR,
            IDLE_TIMEOUT,
        }
    )


#: 每种 command 的 payload 约束。结构校验，与 journal 的 `PAYLOAD_SCHEMAS` 同一套
#: 语义：必填字段必须在、有没有未知字段也要判。
#:
#: 未知字段报错而不是忽略：客户端拼错 key 时静默忽略会得到「成功但什么也没发生」。
COMMAND_SCHEMAS: Final[dict[str, PayloadSchema]] = {
    CommandType.SESSION_OPEN: PayloadSchema(required=frozenset({"credential"})),
    CommandType.USER_MESSAGE: PayloadSchema(required=frozenset({"content"})),
    CommandType.PERMISSION_DECIDE: PayloadSchema(
        required=frozenset({"invocation_id", "status"}),
        optional=frozenset({"decided_by", "reason"}),
    ),
    CommandType.RUN_CANCEL: PayloadSchema(optional=frozenset({"reason", "run_id"})),
    CommandType.STEP_RETRY: PayloadSchema(),
    CommandType.SESSION_STATUS: PayloadSchema(),
    CommandType.TRACE_LIST: PayloadSchema(),
    CommandType.TRACE_GET: PayloadSchema(required=frozenset({"trace_id"})),
    CommandType.PING: PayloadSchema(),
}

#: 信封里除 `payload` 之外的字段。信封也不许有多余字段。
_ENVELOPE_KEYS: Final = frozenset({"v", "type", "id", "session_id", "payload"})


@dataclass(frozen=True, slots=True)
class Command:
    """客户端发来的一条命令。

    `session_id` 可空：只有 `session.open` 没有，其余 command 缺它是协议错误 ——
    服务端拿不到 session 就没有隔离边界。
    """

    id: str
    type: str
    session_id: str | None
    payload: JsonObject

    @classmethod
    def parse(cls, raw: JsonValue) -> Command:
        """解一帧上行命令。字段变更按破坏性变更处理。"""
        if not isinstance(raw, dict):
            raise WireProtocolError(f"命令必须是对象，得到 {type(raw).__name__}", code="bad_frame")
        _check_version(raw)
        unknown = set(raw) - _ENVELOPE_KEYS
        if unknown:
            raise WireProtocolError(f"信封有未知字段 {sorted(unknown)}", code="bad_frame")
        type_ = raw.get("type")
        if not isinstance(type_, str):
            raise WireProtocolError(f"上行帧缺 type，得到 {type_!r}", code="bad_frame")
        cmd_id = raw.get("id")
        if not isinstance(cmd_id, str) or not cmd_id:
            raise WireProtocolError(f"命令 id 必须是非空字符串，得到 {cmd_id!r}", code="bad_frame")
        if len(cmd_id) > MAX_COMMAND_ID_LENGTH:
            raise WireProtocolError(
                f"命令 id 超过 {MAX_COMMAND_ID_LENGTH} 字符: {len(cmd_id)}", code="bad_frame"
            )
        if type_ not in CommandType.ALL:
            raise WireProtocolError(f"未知 command {type_!r}", code="unknown_command")
        session_id = raw.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise WireProtocolError(
                f"session_id 必须是字符串，得到 {type(session_id).__name__}",
                code="bad_frame",
            )
        if type_ in CommandType.ANONYMOUS:
            if session_id is not None:
                raise WireProtocolError(
                    f"{type_} 不带 session_id（要绑定的正是它）", code="bad_frame"
                )
        elif not session_id:
            raise WireProtocolError(f"{type_} 必须带 session_id", code="bad_frame")
        payload = _check_payload(type_, raw.get("payload", {}))
        return cls(id=cmd_id, type=type_, session_id=session_id, payload=payload)

    def to_json(self) -> JsonObject:
        """上行编码。测试与实现共用，保证两端一致。"""
        out: JsonObject = {
            "v": WireVersion.CURRENT,
            "type": self.type,
            "id": self.id,
            "payload": self.payload,
        }
        if self.session_id is not None:
            out["session_id"] = self.session_id
        return out


@dataclass(frozen=True, slots=True)
class EventFrame:
    """下发给客户端的一条事件。

    活的投递，不是 journal 记录的转发 —— 所以这里没有游标：客户端断线期间发生的
    事件不会补发。
    """

    session_id: str
    event_type: str
    occur_dt: str | None
    payload: JsonObject

    @classmethod
    def of(cls, event: AgentEvent) -> EventFrame:
        return cls(
            session_id=event.scope.session_id,
            event_type=event.type,
            occur_dt=event.occur_dt,
            payload=event.payload,
        )

    def to_json(self) -> JsonObject:
        return {
            "v": WireVersion.CURRENT,
            "type": FrameType.EVENT,
            "session_id": self.session_id,
            "occur_dt": self.occur_dt,
            "event_type": self.event_type,
            "payload": self.payload,
        }


def ack(command_id: str, *, session_id: str | None = None) -> JsonObject:
    """session 绑定成功，或命令已可靠接纳；执行结果另行通知。"""
    frame: JsonObject = {"v": WireVersion.CURRENT, "type": FrameType.ACK, "id": command_id}
    if session_id is not None:
        frame["session_id"] = session_id
    return frame


def pong(command_id: str) -> JsonObject:
    """`ping` 的回声。兼作应用层心跳：连接两侧据此知道对方还活着。"""
    return {
        "v": WireVersion.CURRENT,
        "type": FrameType.PONG,
        "id": command_id,
    }


def error_frame(command_id: str | None, code: str, message: str) -> JsonObject:
    """一帧错误。`id` 有值表示这条 command 收到了但没办成。

    消息是给人看的，客户端的逻辑只能依赖 `code`。
    """
    return {
        "v": WireVersion.CURRENT,
        "type": FrameType.ERROR,
        "id": command_id,
        "code": code,
        "message": message,
    }


def close_frame(reason: str, message: str = "") -> JsonObject:
    """服务端主动断开前先发这个，让客户端知道为什么。"""
    return {
        "v": WireVersion.CURRENT,
        "type": FrameType.CLOSE,
        "reason": reason,
        "message": message,
    }


def _check_version(raw: JsonObject) -> None:
    version = raw.get("v")
    if not isinstance(version, int) or isinstance(version, bool):
        raise WireProtocolError(f"协议版本必须是整数，得到 {version!r}", code="bad_version")
    if version != WireVersion.CURRENT:
        raise WireProtocolError(
            f"不支持的协议版本 {version}，当前 {WireVersion.CURRENT}",
            code="unsupported_version",
        )


def _check_payload(type_: str, payload: JsonValue) -> JsonObject:
    if not isinstance(payload, dict):
        raise WireProtocolError(
            f"{type_} 的 payload 必须是对象，得到 {type(payload).__name__}",
            code="bad_payload",
        )
    schema = COMMAND_SCHEMAS[type_]
    unknown = set(payload) - schema.allowed()
    if unknown:
        raise WireProtocolError(
            f"{type_} 的 payload 有未知字段 {sorted(unknown)}", code="bad_payload"
        )
    missing = schema.required - set(payload)
    if missing:
        raise WireProtocolError(f"{type_} 的 payload 缺 {sorted(missing)}", code="bad_payload")
    return payload


__all__ = [
    "COMMAND_SCHEMAS",
    "MAX_COMMAND_ID_LENGTH",
    "CloseReason",
    "Command",
    "CommandType",
    "EventFrame",
    "FrameType",
    "WireVersion",
    "ack",
    "close_frame",
    "error_frame",
    "pong",
]
