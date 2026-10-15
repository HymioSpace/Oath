"""span 的对外投影与耗时聚合。

只做形状转换，不读存储 —— 传进来的是 `TraceStore` 已经给好的 tuple[Span, ...]。
"""

from __future__ import annotations

from core.protocol.datatype.trace import Span
from core.utils.encoding import JsonValue


def view(spans: tuple[Span, ...]) -> list[JsonValue]:
    """span 的 wire 形状，附上按 `start / end` 算的毫秒耗时。"""
    return [{**span.to_dict(), "duration_ms": span.duration_ms} for span in spans]


def elapsed(spans: tuple[Span, ...]) -> float | None:
    """trace 的累计耗时：`max(end) - min(start)`。

    一个还没结束的 span 贡献不了结束时刻，所以一条都没结束就是 `None`。
    """
    ends = [span.end_unix_nano for span in spans if span.end_unix_nano is not None]
    if not ends:
        return None
    return round((max(ends) - min(span.start_unix_nano for span in spans)) / 1_000_000, 3)


__all__ = ["elapsed", "view"]
