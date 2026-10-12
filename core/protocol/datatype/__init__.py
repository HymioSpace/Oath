"""core 的数据类型。

顶层是跨模块通用的类型（`Scope` / `Message` / `AgentState`），`journal.py` 是
journal 专属的那些。
"""

from __future__ import annotations

from core.protocol.datatype.agent_state import AgentState
from core.protocol.datatype.cursor import Cursor, CursorKind
from core.protocol.datatype.journal import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
    PAYLOAD_SCHEMAS,
    STATE_PAYLOAD_SCHEMAS,
    AppendResult,
    EventRecord,
    EventType,
    PayloadSchema,
    Record,
    RecordFormat,
    Recovery,
    StateRecord,
    StateRef,
    ToolOutcome,
    is_open_family,
)
from core.protocol.datatype.message import Message
from core.protocol.datatype.scope import SCOPE_ID_PATTERN, Scope, is_valid_scope_id

__all__ = [
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "PAYLOAD_SCHEMAS",
    "SCOPE_ID_PATTERN",
    "STATE_PAYLOAD_SCHEMAS",
    "AgentState",
    "AppendResult",
    "Cursor",
    "CursorKind",
    "EventRecord",
    "EventType",
    "Message",
    "PayloadSchema",
    "Record",
    "RecordFormat",
    "Recovery",
    "Scope",
    "StateRecord",
    "StateRef",
    "ToolOutcome",
    "is_open_family",
    "is_valid_scope_id",
]
