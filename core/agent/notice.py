"""把这一批要告诉模型的事写成一个 user message。

一次 `step` 的输入不是一句裸的用户输入，是「用户说了什么」加上「哪些迟到的工具结果得补
告诉模型」。两样都用 `<events>` 包成一个信封 —— 模型看到的是发生了什么，不是一段猜不出
边界的话。

```xml
<events>
<event_type=user>你好</>
<event_type=tool_result invocation_id="00000002" tool="echo">
之前 id 为 00000002 的工具结果为：链路通了</>
</events>
```

正文一律转义。用户输入里出现 `</>` 就该被读成文本，不是标签。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple
from xml.sax.saxutils import escape, quoteattr

_LINE_TERMINATOR = "\n"


class LateResult(NamedTuple):
    """一个迟到得没法回头填槽位的工具结果。"""

    invocation_id: str
    tool: str
    text: str


def render_notice(contents: Sequence[str], results: Sequence[LateResult]) -> str:
    """拼出 user message 的正文。`contents` 里有几条用户输入就包几个 `user` 块。"""
    lines = ["<events>"]
    for content in contents:
        lines.append(f"<event_type=user>{escape(content)}</>")
    for result in results:
        attrs = f" invocation_id={quoteattr(result.invocation_id)} tool={quoteattr(result.tool)}"
        body = escape(f"之前 id 为 {result.invocation_id} 的工具结果为：{result.text}")
        lines.append(f"<event_type=tool_result{attrs}>{body}</>")
    lines.append("</events>")
    return _LINE_TERMINATOR.join(lines)


__all__ = ["LateResult", "render_notice"]
