"""buffer 的存储后端。"""

from __future__ import annotations

from core.buffer.impl.file_buffer import FileEventBuffer

__all__ = ["FileEventBuffer"]
