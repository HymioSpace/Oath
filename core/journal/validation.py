"""记录的结构校验。

只做**结构**校验：必填字段在不在、有没有未知字段、版本号认不认识。
不做业务语义校验（「这个 tool 名存不存在」不归这里管）。

读取路径和写入路径共用这里的函数，保证「写进去能读出来」这件事由同一套
规则保证，不会出现写入时放过、读取时才炸的分叉。
"""

from __future__ import annotations

import json

from core.journal.cursor import require_object
from core.protocol.datatype import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
    PAYLOAD_SCHEMAS,
    STATE_PAYLOAD_SCHEMAS,
    EventRecord,
    EventType,
    PayloadSchema,
    Scope,
    StateRecord,
    ToolOutcome,
)
from core.protocol.error import (
    CorruptRecordError,
    PayloadSchemaError,
    PayloadVersionError,
    ScopeError,
    ValidationError,
)
from core.utils.encoding import JsonObject, parse_timestamp

_EVENT_FIELDS = frozenset(
    {
        "user_id",
        "session_id",
        "id",
        "type",
        "payload",
        "payload_schema_version",
        "idempotency_key",
        "occur_dt",
        "commit_dt",
    }
)
_STATE_FIELDS = frozenset(
    {
        "user_id",
        "session_id",
        "id",
        "commit_dt",
        "payload_schema_version",
        "payload",
        "cursor_start",
        "cursor_end",
    }
)


def validate_payload(type_: str, version: int, payload: JsonObject) -> None:
    """校验 payload 结构。版本不认识就直接抛 —— 不降级成跳过。"""
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PayloadVersionError(f"payload_schema_version 必须是从 1 开始的整数，得到 {version!r}")
    schema = PAYLOAD_SCHEMAS.get((type_, version))
    if schema is None:
        raise PayloadVersionError(f"未知的 (type, payload_schema_version)=({type_!r}, {version})")
    _check_shape(payload, schema, f"{type_}@v{version}")


def _check_shape(payload: JsonObject, schema: PayloadSchema, what: str) -> None:
    present = set(payload)
    missing = schema.required - present
    if missing:
        raise PayloadSchemaError(f"{what} 缺少必填字段: {sorted(missing)}")
    unknown = present - schema.allowed()
    if unknown:
        raise PayloadSchemaError(f"{what} 含未知字段: {sorted(unknown)}")
    nullable_missing = schema.nullable - present - schema.required
    if nullable_missing:
        raise PayloadSchemaError(f"{what} 缺少必填字段: {sorted(nullable_missing)}")
    if "outcome" in present and payload["outcome"] not in ToolOutcome.ALL:
        raise PayloadSchemaError(f"{what} 的 outcome 非法: {payload['outcome']!r}")


def validate_state_payload(version: int, payload: JsonObject) -> None:
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PayloadVersionError(
            f"状态 payload_schema_version 必须是从 1 开始的整数，得到 {version!r}"
        )
    if version not in STATE_PAYLOAD_SCHEMAS:
        raise PayloadVersionError(f"未知的状态 payload_schema_version={version}")
    if not isinstance(payload, dict):
        raise PayloadSchemaError(f"状态 payload 必须是 JSON 对象，得到 {type(payload).__name__}")


def validate_idempotency_key(key: str) -> None:
    if not isinstance(key, str) or not key:
        raise ValidationError("idempotency_key 不能为空")
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValidationError(f"idempotency_key 超过 {MAX_IDEMPOTENCY_KEY_LENGTH} 字符: {len(key)}")


def validate_occur_dt(occur_dt: str | None) -> None:
    """校验调用方给的业务发生时间。

    它不参与任何正确性判断 —— 但它被写进日志了，格式不对会在别的消费者
    那里炸，所以在这里挡一次。
    """
    if occur_dt is None:
        return
    if not isinstance(occur_dt, str):
        raise ValidationError(f"occur_dt 必须是字符串或 null，得到 {type(occur_dt).__name__}")
    try:
        parse_timestamp(occur_dt)
    except ValueError as exc:
        raise ValidationError(f"occur_dt 不是合法 RFC 3339 时刻: {occur_dt!r}") from exc


def validate_event_type(type_: str) -> None:
    if type_ not in EventType.ALL:
        raise ValidationError(f"未知 type: {type_!r}")


# --------------------------------------------------------------------------
# 读取路径
# --------------------------------------------------------------------------


def _loads(text: str, what: str) -> JsonObject:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CorruptRecordError(f"{what} 不是合法 JSON: {exc}") from exc
    return require_object(obj, what)


def _check_common(obj: JsonObject, allowed: frozenset[str], scope: Scope, what: str) -> None:
    if not scope.matches(obj):
        raise CorruptRecordError(
            f"{what} 的 scope ({obj.get('user_id')!r}, {obj.get('session_id')!r}) "
            f"与所在文件 ({scope}) 不一致"
        )
    unknown = set(obj) - allowed
    if unknown:
        raise CorruptRecordError(f"{what} 含未知字段: {sorted(unknown)}")
    if "user_id" not in obj or "session_id" not in obj:
        raise ScopeError(f"{what} 缺少 user_id / session_id")


def parse_event_record(text: str, scope: Scope) -> EventRecord:
    """解析一行事件日志。结构问题一律抛错，绝不静默跳过。"""
    obj = _loads(text, "事件记录")
    _check_common(obj, _EVENT_FIELDS, scope, "事件记录")

    type_ = obj.get("type")
    if not isinstance(type_, str):
        raise CorruptRecordError(f"事件记录 type 必须是字符串，得到 {type_!r}")
    version = obj.get("payload_schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise CorruptRecordError(f"事件记录 payload_schema_version 必须是整数，得到 {version!r}")
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        raise CorruptRecordError(f"事件记录 payload 必须是对象，得到 {type(payload).__name__}")

    commit_dt = obj.get("commit_dt")
    if not isinstance(commit_dt, str):
        raise CorruptRecordError(f"事件记录 commit_dt 必须是字符串，得到 {commit_dt!r}")
    try:
        parse_timestamp(commit_dt)
    except ValueError as exc:
        raise CorruptRecordError(f"事件记录 commit_dt 非法: {commit_dt!r}") from exc

    validate_idempotency_key(str(obj.get("idempotency_key")))

    user_id = obj.get("user_id")
    session_id = obj.get("session_id")
    if not isinstance(user_id, str) or not isinstance(session_id, str):
        raise ScopeError("事件记录缺少 user_id / session_id")
    key = obj.get("idempotency_key")
    if not isinstance(key, str) or not key:
        raise CorruptRecordError("事件记录 idempotency_key 缺失")
    occur_dt = obj.get("occur_dt")
    if occur_dt is not None and not isinstance(occur_dt, str):
        raise CorruptRecordError(f"事件记录 occur_dt 必须是字符串或 null，得到 {occur_dt!r}")

    record: EventRecord = {
        "user_id": user_id,
        "session_id": session_id,
        "type": type_,
        "payload": payload,
        "payload_schema_version": version,
        "idempotency_key": key,
        "occur_dt": occur_dt,
        "commit_dt": commit_dt,
    }
    record_id = obj.get("id")
    if isinstance(record_id, int) and not isinstance(record_id, bool):
        record["id"] = record_id
    return record


def parse_state_record(text: str, scope: Scope) -> StateRecord:
    obj = _loads(text, "状态记录")
    _check_common(obj, _STATE_FIELDS, scope, "状态记录")

    version = obj.get("payload_schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise CorruptRecordError(f"状态记录 payload_schema_version 必须是整数，得到 {version!r}")
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        raise CorruptRecordError(f"状态记录 payload 必须是对象，得到 {type(payload).__name__}")
    commit_dt = obj.get("commit_dt")
    if not isinstance(commit_dt, str):
        raise CorruptRecordError(f"状态记录 commit_dt 必须是字符串，得到 {commit_dt!r}")

    user_id = obj.get("user_id")
    session_id = obj.get("session_id")
    if not isinstance(user_id, str) or not isinstance(session_id, str):
        raise ScopeError("状态记录缺少 user_id / session_id")

    record: StateRecord = {
        "user_id": user_id,
        "session_id": session_id,
        "commit_dt": commit_dt,
        "payload_schema_version": version,
        "payload": payload,
        "cursor_start": obj.get("cursor_start"),
        "cursor_end": require_object(obj.get("cursor_end"), "状态记录 cursor_end"),
    }
    record_id = obj.get("id")
    if isinstance(record_id, int) and not isinstance(record_id, bool):
        record["id"] = record_id
    return record


__all__ = [
    "parse_event_record",
    "parse_state_record",
    "validate_event_type",
    "validate_idempotency_key",
    "validate_occur_dt",
    "validate_payload",
    "validate_state_payload",
]
