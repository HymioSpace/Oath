"""时间来源 —— 注入而不是全局单例。

需要「现在」的地方接收一个 `Clock`，不直接从 `time` 模块取。这样测试可以注入
`ManualClock` 任意跳转，不用 sleep，也不用去 monkeypatch `time.time`。

v0.1 的「存储端时间」就是这个注入的 Clock。真正的服务端时间要等独立存储
adapter 才有。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from core.errors import ValidationError


@runtime_checkable
class Clock(Protocol):
    """时间来源。SystemClock 用于生产，ManualClock 用于测试。"""

    def now(self) -> datetime:
        """返回带 UTC 时区的当前时间。必须是 tz-aware，否则跨时区会算错。"""
        ...


class SystemClock:
    """生产用时钟。UTC，tz-aware。"""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def __repr__(self) -> str:
        return "SystemClock()"


class ManualClock:
    """测试用时钟。时间只在测试显式调用 advance / set 时变化。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start if start is not None else datetime(2026, 1, 1, tzinfo=UTC)
        if self._now.tzinfo is None:
            raise ValueError("ManualClock 起点必须是 tz-aware datetime")

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 0.0, *, milliseconds: float = 0.0) -> datetime:
        """向前推进。允许传入负值以测试「deadline 尚未到达」这类边界。"""
        self._now = self._now + timedelta(seconds=seconds, milliseconds=milliseconds)
        return self._now

    def set(self, moment: datetime) -> datetime:
        self._now = moment
        return self._now

    def __repr__(self) -> str:
        return f"ManualClock({self._now.isoformat()})"


def to_iso(moment: datetime) -> str:
    """统一的时间序列化格式。

    一律转成 UTC 再去掉时区后缀。`+08:00` 和 `Z` 是同一时刻但字节不同，
    而 checksum、payload_hash、cursor 都要求字节级稳定。

    naive datetime 直接拒绝：`astimezone()` 会静默套用本机时区，
    同一时刻在 CI（UTC）和开发机（+08:00）上会算出不同的字符串。
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValidationError(f"to_iso 需要 tz-aware datetime，收到 naive 值: {moment!r}")
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def from_iso(text: str) -> datetime:
    """`to_iso` 的逆操作。解析失败抛 ValueError（属于调用方契约违反）。"""
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"时间戳缺少时区信息: {text}")
    return parsed.astimezone(UTC)