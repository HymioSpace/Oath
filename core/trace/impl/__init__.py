"""trace store 的实现。

v1 只有文件。要换后端就新增一个模块，不要改 `core/trace/`。
"""

from __future__ import annotations

from core.trace.impl.file_trace_store import FileTraceStore

__all__ = ["FileTraceStore"]
