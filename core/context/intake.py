"""只使用已认领输入构造模型视图。reminder 不改变真实工具状态。"""

from __future__ import annotations

import copy
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from core.context.events import event_xml
from core.journal.runtime_store import State
from core.protocol.datatype.inbound import Inbound
from core.utils.encoding import canonical_json


def _result_text(payload: State) -> str:
    content = payload.get("content")
    return content if isinstance(content, str) else canonical_json(payload)


def build_messages(state: State, entries: list[tuple[str, Any]], step_id: str) -> list[State]:
    messages: list[State] = copy.deepcopy(state["conversation"].get("messages", []))
    selected = {
        e["item"]["payload"]["invocation_id"]: (key, e["item"]["payload"])
        for key, e in entries
        if e["queue"] == "tool" and e["item"]["source"] != "subagent.start"
    }
    used: set[str] = set()
    for batch_id, batch in state["batches"].items():
        if batch["presented_by_step"] is not None:
            continue
        # 一个 step 只声明一批；未闭合的工具声明必须是当前历史的尾部。
        if not messages or not messages[-1].get("tool_calls"):
            raise ValueError("批次没有对应的 assistant 工具声明")
        declared = [r["id"] for r in messages[-1]["tool_calls"]]
        calls = [state["calls"][i] for i in batch["members"]]
        if declared != [c["tool_call_id"] for c in calls]:
            raise ValueError("批次成员与 assistant 声明不一致")
        for call in calls:
            invocation_id = call["invocation_id"]
            if invocation_id in selected:
                key, payload = selected[invocation_id]
                used.add(key)
                text = _result_text(payload)
            else:
                status = call["status"]
                if status == "waiting_approval":
                    text = "等待用户批准，尚未执行。"
                elif status in {"queued", "running", "admitting"}:
                    text = "原调用尚未返回执行结果。"
                else:
                    text = "结果尚未进入本轮，收到后将补充；不要假定失败或重新执行。"
                text += (
                    f" batch_id={batch_id}; invocation_id={invocation_id}。"
                    "原调用仍有效，后续自动通知，不要重复发起同一动作。"
                )
            messages.append({"role": "tool", "content": text, "tool_call_id": call["tool_call_id"]})
        batch["presented_by_step"] = step_id
        # 这次呈现带的是不是真结果。带占位符不算 —— 真结果到了还要再叫模型一次。
        batch["delivered"] = all(call["invocation_id"] in selected for call in calls)
    events: list[str] = []
    image_refs: list[str] = []
    # Images are application-owned references inside a structured tool result.
    # Add them after every declared tool slot has been closed, as user image parts.
    for _, payload in selected.values():
        result = payload.get("content")
        refs = result.get("image_refs") if isinstance(result, dict) else None
        if isinstance(refs, list) and len(refs) <= 12 and all(
            isinstance(ref, str) and 1 <= len(ref) <= 128 for ref in refs
        ):
            image_refs.extend(refs)
    if image_refs:
        events.append("<tool_images>Images referenced by completed tool results.</tool_images>")

    def flush_events() -> None:
        if events:
            message: State = {"role": "user", "content": "<events>" + "".join(events) + "</events>"}
            if image_refs:
                message["image_refs"] = list(dict.fromkeys(image_refs))
            messages.append(message)
            events.clear()
            image_refs.clear()

    for key, entry in entries:
        item = Inbound.from_dict(entry["item"])
        if entry["queue"] != "tool" or item.source == "subagent.start":
            events.append(event_xml(item, entry.get("buffered_at")))
            if item.source == "user.message":
                refs = item.payload.get("image_refs", [])
                if (
                    not isinstance(refs, list)
                    or len(refs) > 12
                    or any(not isinstance(ref, str) or not 1 <= len(ref) <= 128 for ref in refs)
                ):
                    raise ValueError("Invalid image references")
                image_refs.extend(ref for ref in refs if isinstance(ref, str))
        elif key not in used:
            flush_events()
            p = item.payload
            attrs = " ".join(
                f"{name}={quoteattr(str(p.get(name, '')))}"
                for name in ("event_id", "batch_id", "invocation_id", "outcome")
            )
            messages.append(
                {
                    "role": "user",
                    "content": f"<tool_result {attrs}>{escape(_result_text(p))}</tool_result>",
                }
            )
    flush_events()
    validate_messages(messages)
    return messages


def validate_messages(messages: list[State]) -> None:
    expected: list[str] = []
    for message in messages:
        if expected:
            if message["role"] != "tool" or message.get("tool_call_id") != expected.pop(0):
                raise ValueError("tool call / result 必须连续且逐项匹配")
        elif message["role"] == "tool":
            raise ValueError("孤立 tool result")
        refs = message.get("tool_calls", [])
        if refs:
            expected = [r["id"] for r in refs]
            if len(set(expected)) != len(expected) or any(not i for i in expected):
                raise ValueError("tool_call_id 缺失或重复")
    if expected:
        raise ValueError("未闭合的工具声明")
