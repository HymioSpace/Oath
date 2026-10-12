"""执行后端。

v1 只有线程池。要换执行模型（进程池、asyncio）就新增一个模块，不要改
`core/tool/`。
"""

from __future__ import annotations

from core.tool.impl.threaded_tool_system import AWAITING_APPROVAL_CONTENT, ThreadedToolSystem

__all__ = ["AWAITING_APPROVAL_CONTENT", "ThreadedToolSystem"]
