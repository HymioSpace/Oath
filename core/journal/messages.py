"""messages 投影：把事件记录投影成 LLM 对话历史。

规则由 type 表决定，不含任何 agent runtime 的业务逻辑，也不知道任何一家
provider 的字段名。投影顺序 = 游标顺序，不看时间戳。
"""

from __future__ import annotations

from core.protocol.datatype import Cursor, EventType, Message, Record
from core.utils.encoding import JsonObject, canonical_json


def _tool_content(payload: JsonObject) -> str:
    """工具结果的文本形态。

    有 `content` 且是字符串就用它，否则把整个 payload 序列化 —— 结果不能
    因为「没塞 content」就变成空串，那会让模型以为工具什么也没返回。
    """
    content = payload.get("content")
    if isinstance(content, str):
        return content
    return canonical_json(payload)


def project(records: list[Record], cursor_end: Cursor | None = None) -> list[Message]:
    """投影出截至 `cursor_end` 的消息数组。`cursor_end` 为 None 表示投影全部。

    调用 `recover()` 时传 `Recovery.state.cursor_end`，保证 messages 与 state 对齐
    同一水位线；折叠 `pending` 之后想补齐完整历史时传 None。

    - `user.message` / `assistant.message` 直接产出消息。
    - `tool.call` 先占一个位置，对应的 `tool.result` 到达时填进去。这样重试
      （多次 attempt 产出多条 result）只会保留最后一条终态，不会在对话里
      堆出多条 tool 消息。
    - 到了 `cursor_end` 仍未收到 result 的 `tool.call`（快照提交在飞行中途）
      产出一条 `unresolved` 的 tool 消息。**不静默丢掉** —— 丢掉会让 assistant
      轮带着 `tool_calls` 却没有对应结果，这个数组交给任何 provider 都是坏的。
    - `system.*` 与其余记录不投影，只留在日志里。
    """
    slots: list[Message | None] = []
    call_slot: dict[str, int] = {}

    for record in records:
        if cursor_end is not None and record.cursor.value > cursor_end.value:
            break
        type_ = record.type
        if type_ == EventType.USER_MESSAGE:
            slots.append(Message(role="user", content=str(record.payload.get("content", ""))))
        elif type_ == EventType.ASSISTANT_MESSAGE:
            extra: dict[str, object] = {}
            if record.payload.get("reasoning") is not None:
                extra["reasoning"] = record.payload["reasoning"]
            if record.payload.get("tool_calls") is not None:
                extra["tool_calls"] = record.payload["tool_calls"]
            content = str(record.payload.get("content", ""))
            slots.append(Message(role="assistant", content=content, extra=extra))
        elif type_ == EventType.TOOL_CALL:
            invocation_id = str(record.payload.get("invocation_id", ""))
            call_slot[invocation_id] = len(slots)
            slots.append(None)
        elif type_ == EventType.TOOL_RESULT:
            invocation_id = str(record.payload.get("invocation_id", ""))
            index = call_slot.get(invocation_id)
            if index is None:
                # 合法的日志里不会出现：result 一定跟在 call 后面。
                # 跳过而不是凭空造一条 tool 消息 —— 没有对应 assistant
                # tool_calls 的 tool 消息比缺一条更糟。
                continue
            slots[index] = Message(
                role="tool",
                content=_tool_content(record.payload),
                extra={"tool_call_id": invocation_id, "outcome": record.payload.get("outcome")},
            )

    unresolved = {i for i in call_slot.values() if slots[i] is None}
    for index in unresolved:
        invocation_id = next(k for k, v in call_slot.items() if v == index)
        slots[index] = Message(
            role="tool",
            content="",
            extra={"tool_call_id": invocation_id, "unresolved": True},
        )

    return [m for m in slots if m is not None]


__all__ = ["project"]
