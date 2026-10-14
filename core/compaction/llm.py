"""`LlmCompactor` —— 用一次模型调用把一段历史压成摘要。

输出是固定结构的 XML，让「必须逐字保留的东西」和「进展叙述」分开：
摘要负责让模型知道发生过什么，`facts` 负责别把标识符和数字丢了。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from core.protocol.datatype.llm import LlmRequest
from core.protocol.datatype.message import Message
from core.protocol.interface.compaction import Compactor
from core.protocol.interface.llm import ModelProvider

_INSTRUCTION = """\
你把一段对话历史压缩成一条摘要，供同一个助手接着往下推理。只输出下面这个 XML，不要解释：
<context_digest>
<summary>已完成的进展，按时间顺序。</summary>
<facts>
<item>必须逐字保留的东西：标识符、数字、用户原话的要求、约束。</item>
</facts>
<open>
<item>还没闭环的动作、还没回答的问题。</item>
</open>
</context_digest>

规则：只保留后续推理真正需要的信息；不编造原文没有的内容；
工具结果的原文不用复述，提到它做了什么、key 是什么即可。
facts 和 open 可以为空，但 summary 不能空。"""


class LlmCompactor(Compactor):
    """一次模型调用。模型失败或输出不成形就返回 `None`，调用方保留原历史。"""

    def __init__(self, provider: ModelProvider, *, model: str, max_output_tokens: int) -> None:
        self._provider = provider
        self._model = model
        self._max_output_tokens = max_output_tokens

    def compress(
        self, messages: Sequence[Mapping[str, Any]], *, budget_tokens: int
    ) -> list[dict[str, Any]] | None:
        response = self._provider.complete(
            LlmRequest(
                messages=[
                    Message("system", _INSTRUCTION),
                    Message("user", transcript(messages)),
                ],
                model=self._model,
                max_output_tokens=min(budget_tokens, self._max_output_tokens),
                temperature=0,
            )
        )
        digest = _parse(response.content)
        return [digest] if digest else None


def transcript(messages: Sequence[Mapping[str, Any]]) -> str:
    """把消息渲染成纯文本。序号是原下标，摘要有疑问时还能对上。"""
    lines = []
    for index, message in enumerate(messages, start=1):
        role = str(message.get("role", ""))
        lines.append(f"[{index}] {role}: {message.get('content', '')}")
        for call in message.get("tool_calls") or ():
            lines.append(f"[{index}] {role} tool_calls: {call.get('name')} {call.get('arguments')}")
    return "\n".join(lines)


def _parse(text: str) -> dict[str, Any] | None:
    """解析 XML 摘要。不成形或没有正文就返回 `None`。"""
    body = text.strip()
    if body.startswith("```"):
        lines = body.splitlines()
        body = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None
    summary = (root.findtext("summary") or "").strip()
    if not summary:
        return None
    parts = [f"<context_summary>\n{escape(summary)}"]
    for tag, title in (("facts", "必须记住"), ("open", "还没闭环")):
        items = [f"- {escape(text)}" for text in _items(root, tag)]
        if items:
            parts.append(f"{title}：\n" + "\n".join(items))
    parts.append("</context_summary>")
    return {"role": "user", "content": "\n".join(parts)}


def _items(root: ElementTree.Element, tag: str) -> list[str]:
    return [
        (item.text or "").strip()
        for item in root.findall(f"{tag}/item")
        if (item.text or "").strip()
    ]


__all__ = ["LlmCompactor", "transcript"]
