"""队列的实现。协议层的 `EventQueue` 在 `core/protocol/interface/queue.py`。"""

from __future__ import annotations

from core.queue.in_memory_queue import InMemoryEventQueue

__all__ = ["InMemoryEventQueue"]
