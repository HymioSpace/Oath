"""可观测记录：一次 ReAct 循环的 span 流水。

依赖方向：本模块依赖 `core/protocol` 与 `core/utils`，反过来没有。
放哪个、怎么放由 `TraceStore` 的实现方决定。
"""

from __future__ import annotations

from core.trace.attributes import chat_span_name, input_event_name

__all__ = ["chat_span_name", "input_event_name"]
