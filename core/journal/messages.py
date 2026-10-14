"""messages 投影：把事件记录投影成 LLM 对话历史。

规则由 type 表决定，不含任何 agent runtime 的业务逻辑，也不知道任何一家
provider 的字段名。投影顺序 = 游标顺序，不看时间戳。

**槽位由 `assistant.message` 声明，不由 `tool.call` 记录决定。** 模型那一轮说了要调哪
几个工具，那些槽位就得在那条消息之后挨着排好 —— 中间崩了、没来得及写 `tool.call`，
留下的数组照样是能交给 provider 的。`tool.call` 记录后来补上时认领同一个槽位。

**迟到的结果进补告，不改历史。** 槽位已经填过、模型也已经答过的那一轮，结果回来
只能换一种说法告诉它，不能回头去改那个槽位 —— 那个占位是模型读过的记录。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr

from core.protocol.datatype import Cursor, EventType, Message, Record
from core.utils.encoding import JsonObject, JsonValue, canonical_json

_LINE_TERMINATOR = "\n"


class LateResult:
    """一个迟到得没法回头填槽位的工具结果。"""

    __slots__ = ("invocation_id", "text", "tool")

    def __init__(self, invocation_id: str, tool: str, text: str) -> None:
        self.invocation_id = invocation_id
        self.tool = tool
        self.text = text


def render_notice(results: list[LateResult]) -> str:
    """把迟到结果拼成一条 user message 的正文。

    模型看到的是「发生了什么」，不是一段猜不出边界的话。正文一律转义 —— 工具
    返回里出现 `</>` 就该被读成文本，不是标签。
    """
    lines = ["<events>"]
    for result in results:
        attrs = f" invocation_id={quoteattr(result.invocation_id)} tool={quoteattr(result.tool)}"
        body = escape(f"之前 id 为 {result.invocation_id} 的工具结果为：{result.text}")
        lines.append(f"<event_type=tool_result{attrs}>{body}</>")
    lines.append("</events>")
    return _LINE_TERMINATOR.join(lines)


def tool_content(payload: JsonObject) -> str:
    """工具结果的文本形态。

    有 `content` 且是字符串就用它，否则把整个 payload 序列化 —— 结果不能
    因为「没塞 content」就变成空串，那会让模型以为工具什么也没返回。
    """
    content = payload.get("content")
    if isinstance(content, str):
        return content
    return canonical_json(payload)


@dataclass(slots=True)
class _Slot:
    """一个工具槽位。位置在开出来的时候就定了，之后只会被填，不会挪。"""

    #: 在输出数组里的下标。
    index: int
    #: 模型那边的 id —— 填进 tool 消息的 `tool_call_id` 就是它。
    tool_call_id: str
    tool: str
    #: 后面又有一条 `assistant.message` 落下来了：模型已经读过这个槽位。
    closed: bool = False
    result: Record | None = None


@dataclass(slots=True)
class _Slots:
    """一轮投影里所有槽位的登记处。"""

    slots: list[_Slot] = field(default_factory=list)
    by_invocation: dict[str, _Slot] = field(default_factory=dict)
    by_call_id: dict[str, _Slot] = field(default_factory=dict)

    def open(self, out: list[Message | None], tool_call_id: str, tool: str) -> _Slot:
        slot = _Slot(index=len(out), tool_call_id=tool_call_id, tool=tool)
        out.append(None)
        self.slots.append(slot)
        if tool_call_id:
            self.by_call_id[tool_call_id] = slot
        return slot

    def close_all(self) -> None:
        for slot in self.slots:
            slot.closed = True


def project(records: list[Record], cursor_end: Cursor | None = None) -> list[Message]:
    """投影出截至 `cursor_end` 的消息数组。`cursor_end` 为 None 表示投影全部。

    调用 `recover()` 时传 `Recovery.state.cursor_end`，保证 messages 与 state 对齐
    同一水位线；要看完整历史时传 None。

    - `user.message` / `assistant.message` 直接产出消息。
    - `assistant.message` 带 `tool_calls` 时，每个调用紧跟着开一个槽位。`tool.call`
      记录到达时认领它，`tool.result` 到达时填进去 —— 同一 `invocation_id` 上的多条
      结果只保留最后一条，不会在对话里堆出多条 tool 消息。
    - 填进 tool 消息的 `tool_call_id` 取自**模型那轮声明的 id**。`tool.result` 自己也
      带一份，但以 call 侧为准：配对靠 `invocation_id`，而模型只认 `tool_call_id`，
      两者不是一回事。
    - 到了末尾仍未收到 result 的槽位产出一条 `unresolved` 的 tool 消息。**不静默丢掉**
      —— 丢掉会让 assistant 轮带着 `tool_calls` 却没有对应结果，这个数组交给任何
      provider 都是坏的。
    - 槽位已经关掉的 `tool.result` 不填进去，攒成一条 `<events>` 补告挂在最后。
    - 其余 type（`system.agent_step`）不投影，只留在日志里。
    """
    # Runtime 提交携带已经固定的模型视图；原始结果的到达不会回填这些槽位。
    runtime_messages = None
    for record in records:
        if cursor_end is not None and record.cursor.value > cursor_end.value:
            break
        if record.type == EventType.RUNTIME_COMMIT:
            changes = record.payload.get("changes")
            conversation = changes.get("conversation") if isinstance(changes, dict) else None
            if isinstance(conversation, dict) and "messages" in conversation:
                runtime_messages = conversation["messages"]
    if isinstance(runtime_messages, list):
        return [
            Message(
                role=str(m["role"]),
                content=str(m["content"]),
                extra={k: v for k, v in m.items() if k not in {"role", "content"}},
            )
            for m in runtime_messages
            if isinstance(m, dict)
        ]

    out: list[Message | None] = []
    slots = _Slots()
    late: list[LateResult] = []

    for record in records:
        if cursor_end is not None and record.cursor.value > cursor_end.value:
            break
        type_ = record.type
        if type_ == EventType.USER_MESSAGE:
            out.append(Message(role="user", content=str(record.payload.get("content", ""))))
        elif type_ == EventType.ASSISTANT_MESSAGE:
            _close_and_append_assistant(out, slots, record.payload)
        elif type_ == EventType.TOOL_CALL:
            slot = slots.by_call_id.get(str(record.payload.get("tool_call_id", "")))
            if slot is None:
                slot = slots.open(
                    out, str(record.payload.get("tool_call_id", "")), _tool_of(record.payload)
                )
            slot.tool = _tool_of(record.payload) or slot.tool
            slots.by_invocation[str(record.payload.get("invocation_id", ""))] = slot
        elif type_ == EventType.TOOL_RESULT:
            invocation_id = str(record.payload.get("invocation_id", ""))
            slot = slots.by_invocation.get(invocation_id)
            if slot is None:
                # 合法的日志里不会出现：result 一定跟在 call 后面。
                # 跳过而不是凭空造一条 tool 消息 —— 没有对应 assistant
                # tool_calls 的 tool 消息比缺一条更糟。
                continue
            if slot.closed:
                late.append(LateResult(invocation_id, slot.tool, tool_content(record.payload)))
            else:
                slot.result = record

    for slot in slots.slots:
        out[slot.index] = _tool_message(slot)

    messages = [m for m in out if m is not None]
    if late:
        messages.append(Message(role="user", content=render_notice(late)))
    return messages


def _close_and_append_assistant(
    out: list[Message | None], slots: _Slots, payload: JsonObject
) -> None:
    """投影一条 assistant 消息，并把它声明的每个调用开成槽位。

    先关旧槽再开新槽：关掉的是**这条消息之前**的那一批，它们模型已经读过了。
    """
    extra: dict[str, object] = {}
    if payload.get("reasoning") is not None:
        extra["reasoning"] = payload["reasoning"]
    if payload.get("tool_calls") is not None:
        extra["tool_calls"] = payload["tool_calls"]
    out.append(Message(role="assistant", content=str(payload.get("content", "")), extra=extra))
    slots.close_all()
    refs = payload.get("tool_calls")
    for ref in refs if isinstance(refs, list) else ():
        if not isinstance(ref, dict):
            continue
        slots.open(out, str(ref.get("id", "")), str(ref.get("name", "")))


def _tool_message(slot: _Slot) -> Message:
    if slot.result is None:
        return Message(
            role="tool",
            content="",
            extra={"tool_call_id": slot.tool_call_id, "unresolved": True},
        )
    return Message(
        role="tool",
        content=tool_content(slot.result.payload),
        extra={
            "tool_call_id": slot.tool_call_id,
            "outcome": slot.result.payload.get("outcome"),
        },
    )


def _tool_of(payload: JsonObject) -> str:
    value: JsonValue = payload.get("tool")
    return value if isinstance(value, str) else ""


__all__ = ["LateResult", "project", "render_notice", "tool_content"]
