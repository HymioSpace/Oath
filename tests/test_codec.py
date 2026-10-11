"""codec 与 clock 的单元测试。

覆盖点：
- canonical JSON 与字典序无关（同样的键顺序不同的 dict 得到同样的字节）
- 拒绝 float —— 跨语言/跨版本的浮点序列化不稳定，不能进 checksum
- 拒绝非 JSON 类型并给出路径
- to_iso / from_iso 的往返，且强制 UTC
- hash_json 对内容变化敏感
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from core.clock import ManualClock, SystemClock, from_iso, to_iso
from core.codec import assert_json_safe, canonical_bytes, canonical_json, checksum_of, hash_json
from core.errors import ValidationError


class TestCanonicalJson:
    def test_key_order_does_not_matter(self) -> None:
        a = {"b": 1, "a": 2, "c": {"y": 1, "x": 2}}
        b = {"c": {"x": 2, "y": 1}, "a": 2, "b": 1}
        assert canonical_bytes(a) == canonical_bytes(b)

    def test_list_order_does_matter(self) -> None:
        # list 的顺序有意义 —— 记录顺序决定 seq 分配，不能排序
        assert canonical_bytes({"xs": [1, 2]}) != canonical_bytes({"xs": [2, 1]})

    def test_separators_are_compact(self) -> None:
        # 带空格会为同一个内容产生不同的字节，checksum 就失去意义了
        assert canonical_json({"a": 1, "b": 2}) == '{"a":1,"b":2}'

    def test_non_ascii_not_escaped(self) -> None:
        # ensure_ascii=False：中文内容按原文入字节。转成 \uXXXX 也能往返，
        # 但会让日志体积膨胀、且让人工核对日志变难。
        assert canonical_json({"t": "你好"}) == '{"t":"你好"}'

    def test_utf8_roundtrip(self) -> None:
        data = {"t": "你好", "emoji": "🌱"}
        assert canonical_bytes(data).decode("utf-8")


class TestAssertJsonSafe:
    def test_rejects_float_with_path(self) -> None:
        # TypeError 而不是 ValidationError：codec 层报「不可编码」，journal 校验层
        # 把它转成 ValidationIssue。调用方不该在这里收到业务级错误。
        with pytest.raises(TypeError) as exc:
            assert_json_safe({"outer": {"inner": [1.5]}})
        assert "$.outer.inner[0]" in str(exc.value)

    def test_rejects_non_json_type(self) -> None:
        with pytest.raises(TypeError):
            assert_json_safe({"when": datetime.now()})

    def test_accepts_primitives_and_none(self) -> None:
        assert_json_safe({"a": None, "b": 1, "c": "s", "d": True, "e": [1, {"f": 2}]})

    def test_accepts_iso_timestamps_as_strings(self) -> None:
        # 时间戳必须由 to_iso 转成字符串，不能把 datetime 直接塞进 payload
        assert_json_safe({"at": to_iso(datetime.now(UTC))})


class TestHashing:
    def test_hash_is_content_based(self) -> None:
        assert hash_json({"a": 1, "b": 2}) == hash_json({"b": 2, "a": 1})

    def test_hash_changes_with_content(self) -> None:
        assert hash_json({"a": 1}) != hash_json({"a": 2})

    def test_hash_is_stable_across_calls(self) -> None:
        # checksum 进了存储，必须跨进程稳定
        assert hash_json({"x": [1, 2, 3]}) == hash_json({"x": [1, 2, 3]})

    def test_checksum_of_covers_all_parts(self) -> None:
        base = {"a": 1, "b": 2}
        assert checksum_of(base) != checksum_of({**base, "c": 3})

    def test_checksum_of_empty(self) -> None:
        assert checksum_of({}) == hash_json({})


class TestIsoTimestamps:
    def test_to_iso_forces_utc(self) -> None:
        tz = timezone(timedelta(hours=8))
        moment = datetime(2026, 10, 11, 12, 30, tzinfo=tz)
        # 同一时刻的 +08:00 必须写成 Z，否则同一个提交会算出两个 checksum
        assert to_iso(moment).endswith("Z")
        assert to_iso(moment) == to_iso(moment.astimezone(UTC))

    def test_roundtrip(self) -> None:
        moment = datetime(2026, 10, 11, 3, 4, 5, 123456, tzinfo=UTC)
        assert from_iso(to_iso(moment)) == moment

    def test_from_iso_returns_aware_utc(self) -> None:
        parsed = from_iso("2026-10-11T03:04:05Z")
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)

    def test_from_iso_handles_offset_input(self) -> None:
        # 输入可以带偏移，但内部一律转成 UTC
        assert from_iso("2026-10-11T11:04:05+08:00") == datetime(
            2026, 10, 11, 3, 4, 5, tzinfo=UTC
        )

    def test_naive_datetime_is_rejected(self) -> None:
        # 朴素时间戳的「现在」依赖调用方所在时区，进日志就是埋雷
        with pytest.raises(ValidationError):
            to_iso(datetime(2026, 10, 11, 3, 4, 5))


class TestClocks:
    def test_system_clock_is_aware(self) -> None:
        now = SystemClock().now()
        assert now.tzinfo is not None

    def test_manual_clock_advances(self) -> None:
        clock = ManualClock()
        first = clock.now()
        clock.advance(5)
        assert (clock.now() - first).total_seconds() == 5

    def test_manual_clock_advance_supports_milliseconds(self) -> None:
        clock = ManualClock()
        first = clock.now()
        clock.advance(milliseconds=250)
        assert (clock.now() - first).total_seconds() == 0.25

    def test_manual_clock_is_frozen(self) -> None:
        # 不自动流逝是硬约定 —— 确定性重放要求时间只在测试显式推动时变化。
        clock = ManualClock()
        assert clock.now() == clock.now()

    def test_manual_clock_set_can_move_backwards(self) -> None:
        # 允许回拨：feature02 要测「deadline 尚未到达」这类边界，必须能造出过去的时间。
        clock = ManualClock()
        first = clock.now()
        clock.set(first - timedelta(hours=1))
        assert clock.now() < first

    def test_manual_clock_rejects_naive_start(self) -> None:
        with pytest.raises(ValueError):
            ManualClock(start=datetime(2026, 1, 1))
