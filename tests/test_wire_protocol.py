"""对外 wire 协议的编解码。

上行 command 是信任边界的第一道：解析器必须拒绝一切「看起来能跑但不该跑」的帧。
"""

from __future__ import annotations

import pytest

from core.journal.impl import FileJournal
from core.protocol.datatype import (
    CloseReason,
    Command,
    CommandType,
    Cursor,
    CursorKind,
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
            "payload": {
                "credential": "token-1",
                "resume_after": {"kind": CursorKind.LINE, "value": 12},
            },
        }
    )

    assert command.type == CommandType.SESSION_OPEN
    assert command.session_id is None
    assert command.cursor("resume_after") == Cursor(CursorKind.LINE, 12)
    assert command.cursor("nope") is None


def test_resume_after_must_be_a_cursor() -> None:
    command = Command.parse(
        {
            "v": 1,
            "type": CommandType.SESSION_OPEN,
            "id": "c-0",
            "payload": {"credential": "t", "resume_after": {"kind": "line", "value": -1}},
        }
    )

    with pytest.raises(WireProtocolError) as exc:
        command.cursor("resume_after")
    assert exc.value.code == "bad_payload"


def test_event_frame_is_built_from_a_record(scope: Scope, journal: FileJournal) -> None:
    appended = journal.append(
        scope,
        EventType.USER_MESSAGE,
        {"content": "hi"},
        payload_schema_version=1,
        idempotency_key="u1",
    )
    record = next(journal.read_range(scope, Cursor(CursorKind.LINE, 0)))
    frame = EventFrame.of(scope, record).to_json()
    cursor = frame["cursor"]
    assert isinstance(cursor, dict)
    assert cursor["value"] == appended.cursor.value
    assert frame["payload"] == {"content": "hi"}


def test_event_frame_shape(scope: Scope) -> None:
    frame = EventFrame(
        session_id="s1",
        cursor=Cursor(CursorKind.LINE, 7),
        record_type=EventType.USER_MESSAGE,
        occur_dt="2026-10-13T08:31:04Z",
        payload={"content": "hi"},
    ).to_json()

    assert frame["v"] == WireVersion.CURRENT
    assert frame["type"] == FrameType.EVENT
    assert frame["cursor"] == {"kind": CursorKind.LINE, "value": 7}
    assert frame["record_type"] == EventType.USER_MESSAGE


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


# --------------------------------------------------------------------------
# 下行辅助帧
# --------------------------------------------------------------------------


def test_ack_and_pong_echo_the_command_id() -> None:
    assert ack("c-1")["id"] == "c-1"
    assert ack("c-1")["type"] == FrameType.ACK
    assert pong("c-1")["type"] == FrameType.PONG


def test_error_frame_separates_code_from_message() -> None:
    """客户端的逻辑只能依赖 code，message 是给人看的。"""
    frame = error_frame("c-1", "bad_payload", "content 缺了")
    assert frame["code"] == "bad_payload"
    assert frame["id"] == "c-1"


def test_close_frame_carries_a_known_reason() -> None:
    frame = close_frame(CloseReason.SLOW_CONSUMER, "队列满了")
    assert frame["reason"] in CloseReason.ALL
    assert frame["type"] == FrameType.CLOSE
