"""存储后端。

v1 只有 JSONL。要换实现就新增一个模块，不要改 `core/journal/`。
"""

from __future__ import annotations

from core.journal.impl.file_journal import FileJournal

__all__ = ["FileJournal"]
