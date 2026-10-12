"""`AgentState` 本身。

状态的身份就是它的成员：scope、覆盖区间、内容、版本。所以测的是这五个字段的
取值、比较与复制边界。持久化在 durability 测试里。
"""

from __future__ import annotations

from core.protocol.datatype import AgentState, Cursor, CursorKind, Scope
from core.utils.encoding import JsonObject

_SCOPE = Scope(user_id="u1", session_id="s1")
_OTHER_SCOPE = Scope(user_id="u1", session_id="s2")
_ORIGIN = Cursor(CursorKind.LINE, 0)
_SECOND = Cursor(CursorKind.LINE, 2)


def test_empty_by_default() -> None:
    state = AgentState.covering(_SCOPE, _ORIGIN)
    assert state.payload == {}
    assert state.payload_schema_version == 1
    assert state.cursor_start is None
    assert state.cursor_end == _ORIGIN


def test_members_are_the_state_identity() -> None:
    state = AgentState.covering(_SCOPE, _SECOND, {"turn": 2}, cursor_start=_ORIGIN)
    assert state.scope == _SCOPE
    assert state.cursor_start == _ORIGIN
    assert state.cursor_end == _SECOND
    assert state.payload == {"turn": 2}


def test_get_returns_default_for_missing_key() -> None:
    state = AgentState.covering(_SCOPE, _SECOND, {"turn": 2})
    assert state.get("turn") == 2
    assert state.get("mode") is None
    assert state.get("mode", "auto") == "auto"


def test_updated_keeps_scope_range_and_version() -> None:
    """恢复路径要能纯函数地折叠 pending —— 就地改会把上一个状态毁掉。"""
    original = AgentState.covering(
        _SCOPE, _SECOND, {"turn": 1}, cursor_start=_ORIGIN, payload_schema_version=2
    )
    merged = original.updated({"turn": 2, "mode": "auto"})

    assert original.payload == {"turn": 1}
    assert merged.payload == {"turn": 2, "mode": "auto"}
    assert merged.scope == original.scope
    assert merged.cursor_start == original.cursor_start
    assert merged.cursor_end == original.cursor_end
    assert merged.payload_schema_version == 2


def test_covering_copies_so_the_caller_cannot_reach_in() -> None:
    source: JsonObject = {"turn": 1}
    state = AgentState.covering(_SCOPE, _SECOND, source)
    source["turn"] = 999
    assert state.get("turn") == 1


def test_to_json_carries_the_payload_only() -> None:
    """落盘时区间和版本是记录上的独立列，不编码进 payload。"""
    state = AgentState.covering(_SCOPE, _SECOND, {"turn": 3, "nested": {"a": [1, 2]}, "null": None})
    assert state.to_json() == {"turn": 3, "nested": {"a": [1, 2]}, "null": None}


def test_from_record_rebuilds_every_member() -> None:
    payload: JsonObject = {"turn": 3}
    state = AgentState.from_record(_SCOPE, payload, _ORIGIN, _SECOND, 2)
    assert state.scope == _SCOPE
    assert state.to_json() == payload
    assert state.cursor_start == _ORIGIN
    assert state.cursor_end == _SECOND
    assert state.payload_schema_version == 2


def test_covers_is_inclusive_of_the_watermark() -> None:
    state = AgentState.covering(_SCOPE, _SECOND)
    assert state.covers(_ORIGIN)
    assert state.covers(_SECOND)
    assert not state.covers(Cursor(CursorKind.LINE, 3))


def test_equality_covers_every_member() -> None:
    values: JsonObject = {"turn": 1}
    base = AgentState.covering(_SCOPE, _SECOND, values)
    assert base != AgentState.covering(_OTHER_SCOPE, _SECOND, values)
    assert base != AgentState.covering(_SCOPE, Cursor(CursorKind.LINE, 3), values)
    assert base != AgentState.covering(
        _SCOPE, _SECOND, values, cursor_start=_ORIGIN, payload_schema_version=2
    )
