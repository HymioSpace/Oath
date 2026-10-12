"""`AgentState` —— agent 的可恢复状态。

成员就三个：`cursor_start` / `cursor_end` 是这份状态覆盖的事件日志区间，
`values` 是状态内容。区间属于状态而不是某次调用 —— 交回去的和拿回来的必须是
同一个区间，否则 `messages` 截断的位置和 `state` 生效的位置会错开。

`values` 的内容由 agent runtime 定义，journal 只保证原样存下、原样取回。

不可变，`updated()` 返回新对象 —— 恢复路径要能纯函数地折叠 pending。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.protocol.datatype.cursor import Cursor
from core.utils.encoding import JsonObject, JsonValue


@dataclass(frozen=True, slots=True)
class AgentState:
    cursor_start: Cursor | None
    cursor_end: Cursor
    values: JsonObject = field(default_factory=dict)

    @classmethod
    def covering(
        cls,
        cursor_end: Cursor,
        values: JsonObject | None = None,
        *,
        cursor_start: Cursor | None = None,
    ) -> AgentState:
        """声明一份状态覆盖到 `cursor_end`。

        `cursor_start` 是上一次快照的位置，只是审计信息 —— 恢复只按
        `cursor_end` 截断。
        """
        return cls(cursor_start=cursor_start, cursor_end=cursor_end, values=dict(values or {}))

    @classmethod
    def from_record(
        cls, payload: JsonObject, cursor_start: Cursor | None, cursor_end: Cursor
    ) -> AgentState:
        return cls(cursor_start=cursor_start, cursor_end=cursor_end, values=dict(payload))

    def get(self, key: str, default: JsonValue = None) -> JsonValue:
        return self.values.get(key, default)

    def updated(self, values: JsonObject) -> AgentState:
        """合并内容，覆盖区间不变。"""
        return AgentState(
            cursor_start=self.cursor_start,
            cursor_end=self.cursor_end,
            values={**self.values, **values},
        )

    def covers(self, cursor: Cursor) -> bool:
        """这份状态是否已经吃下 `cursor` 处的记录。"""
        return cursor.value <= self.cursor_end.value

    def to_json(self) -> JsonObject:
        """状态内容。覆盖区间不编码在这里，落盘时是记录上的 `cursor_start` /
        `cursor_end` 两列。"""
        return dict(self.values)


__all__ = ["AgentState"]
