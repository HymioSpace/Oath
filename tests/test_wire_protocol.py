"""对外 wire 协议的编解码。

上行 command 是信任边界的第一道：解析器必须拒绝一切「看起来能跑但不该跑」的帧。
"""

from __future__ import annotations

import pytest

from core.protocol.datatype import (
    MAX_COMMAND_ID_LENGTH,
    AgentEvent,
    CloseReason,
    Command,
    CommandType,
    EventFrame,
    EventType,
    FrameType,
    Scope,
    WireVersion,
)
from core.protocol.datatype.wire import ack, close_frame, error_frame, pong
from core.protocol.error import WireProtocolError
from core.utils.encoding import JsonObject, JsonValue


def _frame(**over: JsonValue) -> JsonObject:
    frame: JsonObject = {
        "v": WireVersion.CURRENT,
        "type": CommandType.USER_MESSAGE,
        "id": "c-1",
        "session_id": "s1",
        "payload": {"content": "hello"},
    }
    frame.update(over)
    return frame


def _event(scope: Scope) -> AgentEvent:
    return AgentEvent(
        type=EventType.TOOL_RESULT,
        scope=scope,
        tool_call_id="t-1",
        invocation_id="i-1",
        tool="weather",
        payload={"tool_call_id": "t-1", "outcome": "completed"},
        idempotency_key="tool.result:i-1",
        occur_dt="2026-10-13T08:31:04Z",
    )


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------


def test_user_message_round_trip() -> None:
    command = Command.parse(_frame())

    assert command.type == CommandType.USER_MESSAGE
    assert command.session_id == "s1"
    assert Command.parse(command.to_json()) == command


def test_session_open_carries_no_session_id() -> None:
    command = Command.parse(
        {
            "v": WireVersion.CURRENT,
            "type": CommandType.SESSION_OPEN,
            "id": "c-0",
            "payload": {"credential": "token-1"},
        }
    )

    assert command.type == CommandType.SESSION_OPEN
    assert command.session_id is None


def test_session_open_takes_no_resume_cursor() -> None:
    """对外流是活的投递，断线期间的事件不补发 —— 没有可续的位置。"""
    with pytest.raises(WireProtocolError) as exc:
        Command.parse(
            {
                "v": WireVersion.CURRENT,
                "type": CommandType.SESSION_OPEN,
                "id": "c-0",
                "payload": {"credential": "t", "resume_after": {"kind": "line", "value": 12}},
            }
        )
    assert exc.value.code == "bad_payload"


def test_event_frame_is_built_from_an_event(scope: Scope) -> None:
    frame = EventFrame.of(_event(scope)).to_json()

    assert frame["session_id"] == scope.session_id
    assert frame["event_type"] == EventType.TOOL_RESULT
    assert frame["occur_dt"] == "2026-10-13T08:31:04Z"
    assert frame["payload"] == {"tool_call_id": "t-1", "outcome": "completed"}


def test_event_frame_carries_no_cursor(scope: Scope) -> None:
    """event 是活的投递，不是 journal 记录的转发。帧里不该有位置。"""
    frame = EventFrame.of(_event(scope)).to_json()

    assert "cursor" not in frame
    assert frame["type"] == FrameType.EVENT
    assert frame["v"] == WireVersion.CURRENT


# --------------------------------------------------------------------------
# 拒绝
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frame", "code"),
    [
        (_frame(v=2), "unsupported_version"),
        (_frame(v="1"), "bad_version"),
        (_frame(v=None), "bad_version"),
        (_frame(type="event"), "unknown_command"),
        (_frame(id=""), "bad_frame"),
        (_frame(user="u1"), "bad_frame"),
        (_frame(type="user.shout"), "unknown_command"),
        (_frame(session_id=None), "bad_frame"),
        (_frame(payload={"content": "hi", "extra": 1}), "bad_payload"),
        (_frame(payload={}), "bad_payload"),
        (_frame(payload="hi"), "bad_payload"),
    ],
)
def test_bad_frames_are_rejected(frame: JsonObject, code: str) -> None:
    with pytest.raises(WireProtocolError) as exc:
        Command.parse(frame)
    assert exc.value.code == code


def test_non_object_frame_is_rejected() -> None:
    with pytest.raises(WireProtocolError):
        Command.parse(["not", "an", "object"])


def test_session_open_must_not_declare_a_session() -> None:
    """要绑定的正是 session_id，先报了它就等于客户端自称身份。"""
    with pytest.raises(WireProtocolError) as exc:
        Command.parse(
            {
                "v": 1,
                "type": CommandType.SESSION_OPEN,
                "id": "c-0",
                "session_id": "s1",
                "payload": {"credential": "t"},
            }
        )
    assert exc.value.code == "bad_frame"


def test_permission_decide_requires_a_verdict() -> None:
    with pytest.raises(WireProtocolError) as exc:
        Command.parse(
            _frame(
                type=CommandType.PERMISSION_DECIDE,
                payload={"invocation_id": "inv-1"},
            )
        )
    assert exc.value.code == "bad_payload"


def test_ping_takes_no_payload() -> None:
    command = Command.parse(_frame(type=CommandType.PING, session_id="s1", payload={}))
    assert command.payload == {}

    with pytest.raises(WireProtocolError) as exc:
        Command.parse(_frame(type=CommandType.PING, session_id="s1", payload={"content": ""}))
    assert exc.value.code == "bad_payload"


def test_a_command_id_that_would_overflow_the_idempotency_key_is_rejected() -> None:
    """command id 会拼进 `cmd:` 这个幂等键。放任超长，落盘那一步才炸 —— 而那时候
    用户的话已经出了队列。"""
    assert Command.parse(_frame(id="c" * MAX_COMMAND_ID_LENGTH)).id == "c" * (MAX_COMMAND_ID_LENGTH)

    with pytest.raises(WireProtocolError) as exc:
        Command.parse(_frame(id="c" * (MAX_COMMAND_ID_LENGTH + 1)))
    assert exc.value.code == "bad_frame"


# --------------------------------------------------------------------------
# 下行辅助帧
# --------------------------------------------------------------------------


def test_pong_echoes_the_command_id() -> None:
    assert pong("c-1")["id"] == "c-1"
    assert pong("c-1")["type"] == FrameType.PONG


def test_ack_hands_back_the_bound_session() -> None:
    """`session.open` 是客户端唯一一次知道自己 session_id 的机会。"""
    frame = ack("c-0", session_id="s-1")

    assert frame["type"] == FrameType.ACK
    assert frame["id"] == "c-0"
    assert frame["session_id"] == "s-1"


def test_ack_carries_no_cursor() -> None:
    """回执是应用层结果，不是 journal 位置。投递不重放，没有位置可回。"""
    frame = ack("c-0", session_id="s-1")

    assert "cursor" not in frame


def test_error_frame_separates_code_from_message() -> None:
    """客户端的逻辑只能依赖 code，message 是给人看的。"""
    frame = error_frame("c-1", "bad_payload", "content 缺了")
    assert frame["code"] == "bad_payload"
    assert frame["id"] == "c-1"


def test_close_frame_carries_a_known_reason() -> None:
    frame = close_frame(CloseReason.SLOW_CONSUMER, "队列满了")
    assert frame["reason"] in CloseReason.ALL
    assert frame["type"] == FrameType.CLOSE
