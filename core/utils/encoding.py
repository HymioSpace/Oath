"""跨模块共用的编码原语：JSON 基础类型、canonical JSON、时间戳的文本形态。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Final, TypeAlias

#: wire 格式里的 JSON 值类型。递归别名。
JsonValue: TypeAlias = (
    'str | int | float | bool | dict[str, "JsonValue"] | list["JsonValue"] | None'
)
JsonObject: TypeAlias = "dict[str, JsonValue]"


def canonical_json(obj: object) -> str:
    """编码成跨语言逐字节一致的 JSON：键按字典序、无多余空白、非 ASCII 不转义。

    幂等指纹要哈希 payload，任何实现都必须用这个函数，不能各写各的 `json.dumps`。
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def utc_now_iso() -> str:
    """当前 UTC 时刻，RFC 3339，毫秒以下精度，Z 结尾。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def parse_timestamp(value: str) -> datetime:
    """解析 RFC 3339 时刻。带时区偏移的才接受 —— 裸时间戳没有时区语义。"""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"时间戳缺少时区偏移: {value!r}")
    return parsed


#: 时间戳格式的样例，写进文档和测试用。
TIMESTAMP_EXAMPLE: Final = "2026-10-12T08:31:04.123456Z"
