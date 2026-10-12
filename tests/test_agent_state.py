"""`AgentState` 本身。

覆盖区间是 `AgentState` 的成员，所以测的不只是「带走一组 KV」，还有区间本身
的取值、比较和复制边界。持久化在 durability 测试里。
"""

from __future__ import annotations

from core.protocol.datatype import AgentState, Cursor, CursorKind
from core.utils.encoding import JsonObject

_ORIGIN = Cursor(CursorKind.LINE, 0)
_SECOND = Cursor(CursorKind.LINE, 2)


def test_empty_by_default() -> None:
    assert AgentState.covering(_ORIGIN).values == {}
    assert AgentState.covering(_ORIGIN, None).values == {}


def test_get_returns_default_for_missing_key() -> None:
    state = AgentState.covering(_SECOND, {"turn": 2})
    assert state.get("turn") == 2
    assert state.get("mode") is None
    assert state.get("mode", "auto") == "auto"


def test_updated_keeps_the_range() -> None:
    """恢复路径要能纯函数地折叠 pending —— 就地改会把上一个状态毁掉。"""
    original = AgentState.covering(_SECOND, {"turn": 1}, cursor_start=_ORIGIN)
    merged = original.updated({"turn": 2, "mode": "auto"})

    assert original.values == {"turn": 1}
    assert merged.values == {"turn": 2, "mode": "auto"}
    assert merged.cursor_start == _ORIGIN
    assert merged.cursor_end == _SECOND


def test_covering_copies_so_the_caller_cannot_reach_in() -> None:
    source: JsonObject = {"turn": 1}
    state = AgentState.covering(_SECOND, source)
    source["turn"] = 999
    assert state.get("turn") == 1


def test_to_json_carries_the_content_only() -> None:
    """覆盖区间是记录上的两列，不编码进 payload。"""
    state = AgentState.covering(_SECOND, {"turn": 3, "nested": {"a": [1, 2]}, "null": None})
    assert state.to_json() == {"turn": 3, "nested": {"a": [1, 2]}, "null": None}


def test_from_record_is_the_inverse_of_the_record_columns() -> None:
    payload: JsonObject = {"turn": 3}
    state = AgentState.from_record(payload, _ORIGIN, _SECOND)
    assert state.to_json() == payload
    assert state.cursor_start == _ORIGIN
    assert state.cursor_end == _SECOND


def test_covers_is_inclusive_of_the_watermark() -> None:
    state = AgentState.covering(_SECOND)
    assert state.covers(_ORIGIN)
    assert state.covers(_SECOND)
    assert not state.covers(Cursor(CursorKind.LINE, 3))


def test_states_are_compared_including_the_range() -> None:
    values: JsonObject = {"turn": 1}
    assert AgentState.covering(_SECOND, values) != AgentState.covering(
        Cursor(CursorKind.LINE, 3), values
    )
