"""`Cursor` —— 在某个 user 的追加日志里定位一条记录。

水位线是游标而不是时间戳：墙钟会回拨、会撞精度，用时间戳做水位线会静默丢
数据。见 `spec/feature-journal.md`「为什么不用 commit_dt 做水位线」。

单独的模块而不是并进 `journal.py`：它被 `AgentState` 用来记录覆盖区间，
放在这里才不会和 journal 的返回类型互相 import。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from core.utils.encoding import JsonObject, JsonValue


class CursorKind:
    """游标种类。由哪种存储实现盖章，决定游标值的含义。

    v1 只有 `LINE`（JSONL 实现）；`ID` 为 MySQL 实现预留。不同后端的游标
    不可互换，读取时会校验 kind 是否受支持。
    """

    LINE: Final = "line"
    ID: Final = "id"


@dataclass(frozen=True, slots=True)
class Cursor:
    """日志里的一个位置。

    `LINE` 取物理行号（1-based，不计空行），`ID` 取该 user 内的自增 id。
    0 是合法值而不是哨兵对象，表示「第一条记录之前」，所以游标能正常比较。
    """

    kind: str
    value: int

    def to_json(self) -> JsonObject:
        return {"kind": self.kind, "value": self.value}

    @classmethod
    def from_json(cls, obj: JsonValue) -> Cursor:
        if not isinstance(obj, dict):
            raise ValueError(f"游标必须是对象，得到 {type(obj).__name__}")
        kind = obj.get("kind")
        value = obj.get("value")
        if not isinstance(kind, str):
            raise ValueError(f"游标缺少 kind，得到 {kind!r}")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"游标 value 必须是非负整数，得到 {value!r}")
        return cls(kind=kind, value=value)


__all__ = ["Cursor", "CursorKind"]
