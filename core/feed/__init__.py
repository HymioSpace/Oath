"""feed 的实现。协议层的 `EventFeed` 在 `core/protocol/interface/feed.py`。"""

from __future__ import annotations

from core.feed.journal_feed import JournalEventFeed

__all__ = ["JournalEventFeed"]
