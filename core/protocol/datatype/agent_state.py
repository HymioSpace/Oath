"""`AgentState` —— agent 的可恢复状态。

成员是这份状态的完整身份：属于哪个 `scope`、覆盖 `cursor_start` / `cursor_end`
这段事件日志、内容 `payload` 是什么、`payload_schema_version` 是多少。它们全
都是状态本身而不是某次调用的入参 —— 交回去的和拿回来的必然是同一份，写不岔。

`payload` 的内容由 agent runtime 定义，journal 只保证原样存下、原样取回。

不可变，`updated()` 返回新对象 —— 恢复路径要能纯函数地折叠 pending。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.protocol.datatype.cursor import Cursor
from core.protocol.datatype.scope import Scope
from core.utils.encoding import JsonObject, JsonValue


@dataclass(frozen=True, slots=True)
class AgentState:
    scope: Scope
    cursor_start: Cursor | None
    cursor_end: Cursor
    payload: JsonObject = field(default_factory=dict)
    payload_schema_version: int = 1

    @classmethod
    def covering(
        cls,
        scope: Scope,
        cursor_end: Cursor,
        payload: JsonObject | None = None,
        *,
        cursor_start: Cursor | None = None,
        payload_schema_version: int = 1,
    ) -> AgentState:
        """声明一份属于 `scope`、覆盖到 `cursor_end` 的状态。

        `cursor_start` 是上一次快照的位置，只是审计信息 —— 恢复只按
        `cursor_end` 截断。
        """
        return cls(
            scope=scope,
            cursor_start=cursor_start,
            cursor_end=cursor_end,
            payload=dict(payload or {}),
            payload_schema_version=payload_schema_version,
        )

    @classmethod
    def from_record(
        cls,
        scope: Scope,
        payload: JsonObject,
        cursor_start: Cursor | None,
        cursor_end: Cursor,
        payload_schema_version: int,
    ) -> AgentState:
        """从状态记录重建。五个入参就是记录上的五个列。"""
        return cls(
            scope=scope,
            cursor_start=cursor_start,
            cursor_end=cursor_end,
            payload=dict(payload),
            payload_schema_version=payload_schema_version,
        )

    def get(self, key: str, default: JsonValue = None) -> JsonValue:
        return self.payload.get(key, default)

    def updated(self, payload: JsonObject) -> AgentState:
        """合并内容。覆盖区间与版本不变。"""
        return AgentState(
            scope=self.scope,
            cursor_start=self.cursor_start,
            cursor_end=self.cursor_end,
            payload={**self.payload, **payload},
            payload_schema_version=self.payload_schema_version,
        )

    def covers(self, cursor: Cursor) -> bool:
        """这份状态是否已经吃下 `cursor` 处的记录。"""
        return cursor.value <= self.cursor_end.value

    def to_json(self) -> JsonObject:
        """状态内容的副本 —— 落盘时进记录的 `payload` 列。

        覆盖区间和版本不进这里，它们是记录上的独立列。
        """
        return dict(self.payload)


__all__ = ["AgentState"]
