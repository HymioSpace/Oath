"""游标运算与指纹。

水位线是游标而不是时间戳 —— 墙钟会回拨、会撞精度，用时间戳做水位线会静默
丢数据。见 `spec/feature-journal.md`「为什么不用 commit_dt 做水位线」。
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection

from core.protocol.datatype import Cursor
from core.protocol.error import CursorError, PayloadSchemaError
from core.utils.encoding import JsonObject, JsonValue, canonical_json


def check_kind(cursor: Cursor, supported: Collection[str]) -> Cursor:
    """校验游标种类是本实现支持的，否则抛错。

    不同后端的游标不可互换：JSONL 的 `line=128` 和 MySQL 的 `id=128` 指向
    完全不同的记录，静默混用会读出错的区间。
    """
    if cursor.kind not in supported:
        raise CursorError(f"游标种类 {cursor.kind!r} 不受支持，可用: {sorted(supported)}")
    return cursor


def parse_cursor(obj: JsonValue, *, supported: Collection[str]) -> Cursor:
    """从记录里读出游标并校验种类。"""
    try:
        cursor = Cursor.from_json(obj)
    except ValueError as exc:
        raise CursorError(f"游标无法解析: {exc}") from exc
    return check_kind(cursor, supported)


def beginning(kind: str) -> Cursor:
    """空日志的水位线：位置 0，表示「第一条记录之前」。

    0 是合法值而不是哨兵对象，所以游标可以正常比较。
    """
    return Cursor(kind=kind, value=0)


def record_fingerprint(type_: str, payload_schema_version: int, payload: JsonObject) -> str:
    """幂等指纹：`(type, 版本, payload)` 的 canonical JSON 的 sha256。

    幂等键命中时用它区分「同一次调用被重放」和「调用方复用了一个键」。
    编码必须走 `canonical_json`，否则跨实现算不出同一个值。
    """
    material = canonical_json(
        {
            "type": type_,
            "payload": payload,
            "payload_schema_version": payload_schema_version,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def require_object(value: JsonValue, what: str) -> JsonObject:
    if not isinstance(value, dict):
        raise PayloadSchemaError(f"{what} 必须是 JSON 对象，得到 {type(value).__name__}")
    return value


__all__ = [
    "beginning",
    "check_kind",
    "parse_cursor",
    "record_fingerprint",
    "require_object",
]
