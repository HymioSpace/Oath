"""`FilePermissionStore` 的落盘行为：重放、截断的单行边界、损坏即抛。

这些是 JSONL 实现特有的，不进契约套件 —— 换实现后它们要么不需要，要么要换一套
等价的验证。对应 `spec/feature-tool-system.md` 的「失败与恢复」。
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest

from core.protocol.datatype.permission import PermissionRecord, PermissionStatus
from core.protocol.datatype.scope import Scope
from core.protocol.error import PermissionStoreError
from core.tool.permission.impl import FilePermissionStore
from core.tool.permission.impl.jsonl_store import parse_record
from core.utils.encoding import canonical_json

SCOPE = Scope(user_id="u1", session_id="s1")
PATH = Path("u1/s1/permissions.jsonl")


def make(invocation_id: str = "inv-1", **kwargs: object) -> PermissionRecord:
    return PermissionRecord(
        scope=SCOPE,
        tool_call_id="tc-1",
        invocation_id=invocation_id,
        tool="echo",
        arguments={"a": 1},
        status=PermissionStatus.PENDING,
        requested_dt="2026-01-01T00:00:00.000000Z",
        expires_dt="2026-01-01T00:05:00.000000Z",
        **kwargs,  # type: ignore[arg-type]
    )


def approved(record: PermissionRecord) -> PermissionRecord:
    return replace(
        record,
        status=PermissionStatus.APPROVED,
        decided_by="tester",
        decided_dt="2026-01-01T00:01:00.000000Z",
    )


def file_of(root: Path) -> Path:
    return root / PATH


# --------------------------------------------------------------------- 重放


def test_state_is_rebuilt_from_the_log_on_open(tmp_path: Path) -> None:
    """当前状态是打开时 fold 出来的，不是进程内存里攒的 —— 重启后一样。"""
    with FilePermissionStore(tmp_path) as store:
        store.record(make("inv-1"))
        store.record(make("inv-2"))
        store.resolve(approved(make("inv-2")))

    with FilePermissionStore(tmp_path) as reopened:
        assert [r.invocation_id for r in reopened.pending(SCOPE)] == ["inv-1"]
        settled = reopened.get(SCOPE, "inv-2")
        assert settled is not None
        assert settled.status == PermissionStatus.APPROVED
        assert settled.decided_by == "tester"


def test_one_line_per_write(tmp_path: Path) -> None:
    with FilePermissionStore(tmp_path) as store:
        store.record(make("inv-1"))
        store.resolve(approved(make("inv-1")))

    lines = file_of(tmp_path).read_bytes().splitlines()
    assert len(lines) == 2


def test_each_scope_gets_its_own_file(tmp_path: Path) -> None:
    other = Scope(user_id="u1", session_id="s2")
    with FilePermissionStore(tmp_path) as store:
        store.record(make("inv-1"))
        store.record(replace(make("inv-2"), scope=other))

    assert file_of(tmp_path).exists()
    assert (tmp_path / "u1/s2/permissions.jsonl").exists()


# --------------------------------------------------------------------- 截断


def test_a_torn_last_line_is_dropped_on_open(tmp_path: Path) -> None:
    """崩溃时正在写的那一行可能只有半句。丢它，不猜。"""
    path = tmp_path / PATH
    path.parent.mkdir(parents=True)
    good = canonical_json(make("inv-1").to_dict())
    path.write_text(good + "\n" + '{"type": "requested", "invoc', encoding="utf-8")

    with FilePermissionStore(tmp_path) as store:
        assert [r.invocation_id for r in store.pending(SCOPE)] == ["inv-1"]
        # 截掉半行，让下一次写入不会把它接在后面粘成一条垃圾。
        assert file_of(tmp_path).read_bytes() == (good + "\n").encode("utf-8")


def test_a_corrupt_middle_line_raises(tmp_path: Path) -> None:
    """中间的损坏不能静默跳过 —— 跳过的正是「谁批过什么」。"""
    path = tmp_path / PATH
    path.parent.mkdir(parents=True)
    lines = [
        canonical_json(make("inv-1").to_dict()),
        canonical_json(make("inv-2").to_dict()),
        canonical_json(make("inv-3").to_dict()),
    ]
    path.write_text("\n".join([lines[0], "{not json", lines[2]]) + "\n", encoding="utf-8")

    # handle 是惰性打开的，所以损坏在第一次碰到这个 scope 时才浮出来 —— 而不是
    # 构造存储的那一刻。这跟 journal 一致。
    with FilePermissionStore(tmp_path) as store, pytest.raises(PermissionStoreError):
        store.pending(SCOPE)


def test_a_record_from_another_scope_raises(tmp_path: Path) -> None:
    """目录已经按 scope 分了，文件里的 scope 对不上就是有人挪过文件。"""
    path = tmp_path / PATH
    path.parent.mkdir(parents=True)
    stray = replace(make("inv-1"), scope=Scope(user_id="u1", session_id="s2"))
    path.write_text(canonical_json(stray.to_dict()) + "\n", encoding="utf-8")

    with pytest.raises(PermissionStoreError):
        parse_record(path.read_text(encoding="utf-8").strip(), SCOPE)


def test_an_unknown_field_raises(tmp_path: Path) -> None:
    """结构校验比对着固定字段集合 —— 多出来的字段会让 fold 的语义说不清。"""
    path = tmp_path / PATH
    path.parent.mkdir(parents=True)
    raw = dict(make("inv-1").to_dict())
    raw["surprise"] = 1
    path.write_text(canonical_json(raw) + "\n", encoding="utf-8")

    with pytest.raises(PermissionStoreError):
        parse_record(path.read_text(encoding="utf-8").strip(), SCOPE)


def test_an_unparsable_timestamp_raises(tmp_path: Path) -> None:
    """解析不了的时间戳等于没有有效期，而过期判定全靠它。"""
    path = tmp_path / PATH
    path.parent.mkdir(parents=True)
    raw = dict(make("inv-1").to_dict())
    raw["expires_dt"] = "下周三"
    path.write_text(canonical_json(raw) + "\n", encoding="utf-8")

    with pytest.raises(PermissionStoreError):
        parse_record(path.read_text(encoding="utf-8").strip(), SCOPE)


# --------------------------------------------------------------------- 并发


def test_writes_from_many_threads_keep_every_line(tmp_path: Path) -> None:
    """实例级锁 + O_APPEND：并发写的每一行都是完整的一行。"""
    count = 20
    with FilePermissionStore(tmp_path, fsync=False) as store:
        barrier = threading.Barrier(count, timeout=10)

        def write(n: int) -> None:
            barrier.wait()
            store.record(make(f"inv-{n}"))

        threads = [threading.Thread(target=write, args=(n,)) for n in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    lines = file_of(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == count
    with FilePermissionStore(tmp_path) as reopened:
        assert len(reopened.pending(SCOPE)) == count
