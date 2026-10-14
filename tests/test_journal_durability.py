"""损坏与恢复。

这些测试**人工构造**文件损坏，而不是靠杀进程。原因是 `SIGKILL` 杀不掉普通
文件系统页缓存里的数据：`os.write()` 返回之后内容已经在页缓存里，进程死掉
并不会留下半行。真正的半行要靠断电 / 掉电才会出现，本地没法复现。

所以这里用 `truncate()` 直接切出半行来验证读取侧的容忍逻辑；真实进程的
kill 测试在 `test_journal_crash.py`（slow），它验证的是另一件事 ——
「已返回成功的 append 在崩溃后不会消失」。
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from core.journal.impl import FileJournal
from core.protocol.datatype import (
    AgentState,
    Cursor,
    CursorKind,
    EventType,
    Scope,
)
from core.protocol.error import CorruptRecordError, ScopeError


def _events_path(root: Path, scope: Scope) -> Path:
    return root / scope.user_id / scope.session_id / "events.jsonl"


def _state_path(root: Path, scope: Scope) -> Path:
    return root / scope.user_id / scope.session_id / "state.jsonl"


def _write(root: Path, count: int, scope: Scope) -> None:
    with FileJournal(root) as j:
        for i in range(count):
            j.append(
                scope,
                EventType.USER_MESSAGE,
                {"content": f"第 {i} 条"},
                payload_schema_version=1,
                idempotency_key=f"k{i}",
            )


# --------------------------------------------------------------------------
# 截断尾行
# --------------------------------------------------------------------------


def test_torn_tail_is_dropped_and_the_rest_survives(tmp_path: Path, scope: Scope) -> None:
    root = tmp_path / "journal"
    _write(root, 5, scope)
    path = _events_path(root, scope)

    data = path.read_bytes()
    path.write_bytes(data + b'{"user_id":"u1","sessi')  # 崩在写一半

    with FileJournal(root) as j:
        recovery = j.recover(scope)
    assert len(recovery.pending) == 5
    assert [r.payload["content"] for r in recovery.pending] == [f"第 {i} 条" for i in range(5)]


def test_torn_tail_is_truncated_so_later_appends_stay_clean(tmp_path: Path, scope: Scope) -> None:
    root = tmp_path / "journal"
    _write(root, 3, scope)
    path = _events_path(root, scope)
    good = path.read_bytes()
    with path.open("ab") as fh:
        fh.write(b'{"broken')

    with FileJournal(root) as j:
        j.append(
            scope,
            EventType.USER_MESSAGE,
            {"content": "半行之后追加的"},
            payload_schema_version=1,
            idempotency_key="after",
        )

    # 关键：半行被截掉了，新记录没有被粘在它后面变成一条垃圾。
    assert path.read_bytes().startswith(good)
    assert len(path.read_bytes().splitlines()) == 4


def test_whole_first_record_torn_yields_empty_log(tmp_path: Path, scope: Scope) -> None:
    root = tmp_path / "journal"
    _write(root, 1, scope)
    path = _events_path(root, scope)
    path.write_bytes(b'{"user_id":"u1"')

    with FileJournal(root) as j:
        assert j.recover(scope).pending == []
        assert list(j.read_range(scope, Cursor(CursorKind.LINE, 0))) == []


def test_torn_trailing_snapshot_is_ignored_last_complete_one_wins(
    tmp_path: Path, scope: Scope
) -> None:
    root = tmp_path / "journal"
    with FileJournal(root) as j:
        cursor = j.append(
            scope,
            EventType.USER_MESSAGE,
            {"content": "x"},
            payload_schema_version=1,
            idempotency_key="k",
        ).cursor
        j.commit_state(AgentState.covering(scope, cursor, {"turn": 1}))
        j.commit_state(AgentState.covering(scope, cursor, {"turn": 2}))

    # 第三次提交崩在半路。turn 2 是完整 fsync 过的，不该被牵连。
    path = _state_path(root, scope)
    with path.open("ab") as fh:
        fh.write(b'{"user_id":"u1","session')

    with FileJournal(root) as j:
        recovery = j.recover(scope)
    assert recovery.state == AgentState.covering(scope, cursor, {"turn": 2})


def test_state_log_with_only_a_torn_snapshot_reads_as_no_snapshot(
    tmp_path: Path, scope: Scope
) -> None:
    root = tmp_path / "journal"
    with FileJournal(root) as j:
        cursor = j.append(
            scope,
            EventType.USER_MESSAGE,
            {"content": "x"},
            payload_schema_version=1,
            idempotency_key="k",
        ).cursor
        j.commit_state(AgentState.covering(scope, cursor, {"turn": 1}))

    _state_path(root, scope).write_bytes(b'{"user_id":"u1","sessi')

    with FileJournal(root) as j:
        recovery = j.recover(scope)
    # 第一份快照就没写完：没有水位线，全部记录重新处理。
    assert recovery.state == AgentState.covering(scope, Cursor(CursorKind.LINE, 0))
    assert recovery.messages == []
    assert len(recovery.pending) == 1


# --------------------------------------------------------------------------
# 写失败回滚
# --------------------------------------------------------------------------


def _break_writes_after(monkeypatch: pytest.MonkeyPatch, calls: int) -> None:
    """让前 `calls` 次 `os.write` 正常，第 `calls + 1` 次写一半就报错。"""

    real_write = os.write
    seen = 0

    def flaky(fd: int, data: bytes) -> int:
        nonlocal seen
        seen += 1
        if seen <= calls:
            return real_write(fd, data)
        real_write(fd, data[: max(1, len(data) // 2)])
        raise OSError(errno.EIO, "写设备故障")

    monkeypatch.setattr(os, "write", flaky)


def test_a_failed_append_rolls_back_to_the_last_good_record(tmp_path: Path, scope: Scope) -> None:
    """截回的长度得是**字节数**。按字符算，中文正文差出来的部分会把上一条已经
    fsync 过的记录一起截掉 —— 那条记录是已经向调用方返回成功的。"""
    root = tmp_path / "journal"
    with FileJournal(root) as j:
        j.append(
            scope,
            EventType.USER_MESSAGE,
            {"content": "链路" * 8},
            payload_schema_version=1,
            idempotency_key="k0",
        )
    good = _events_path(root, scope).read_bytes()
    assert len(good) > len(good.decode("utf-8"))  # 多字节：字节数确实大于字符数

    with FileJournal(root) as j, pytest.MonkeyPatch.context() as patch:
        _break_writes_after(patch, calls=0)
        with pytest.raises(OSError):
            j.append(
                scope,
                EventType.USER_MESSAGE,
                {"content": "半路失败"},
                payload_schema_version=1,
                idempotency_key="k1",
            )

    assert _events_path(root, scope).read_bytes() == good


def test_the_log_stays_readable_after_a_rolled_back_append(tmp_path: Path, scope: Scope) -> None:
    """回滚之后再写一次不会粘在半行上。"""
    root = tmp_path / "journal"
    with FileJournal(root) as j:
        j.append(
            scope,
            EventType.USER_MESSAGE,
            {"content": "第一"},
            payload_schema_version=1,
            idempotency_key="k0",
        )
    with FileJournal(root) as j, pytest.MonkeyPatch.context() as patch:
        _break_writes_after(patch, calls=0)
        with pytest.raises(OSError):
            j.append(
                scope,
                EventType.USER_MESSAGE,
                {"content": "失败"},
                payload_schema_version=1,
                idempotency_key="k1",
            )

    with FileJournal(root) as j:
        j.append(
            scope,
            EventType.USER_MESSAGE,
            {"content": "第二"},
            payload_schema_version=1,
            idempotency_key="k2",
        )
        assert [r.payload["content"] for r in j.read_range(scope, Cursor(CursorKind.LINE, 0))] == [
            "第一",
            "第二",
        ]


def test_a_failed_snapshot_keeps_the_previous_one(tmp_path: Path, scope: Scope) -> None:
    """状态日志截回 0 会把上一次提交的那份快照抹掉，而那正是重启后唯一读得到的
    状态 —— 水位线归零，全部记录重新处理。"""
    root = tmp_path / "journal"
    with FileJournal(root) as j:
        cursor = j.append(
            scope,
            EventType.USER_MESSAGE,
            {"content": "x"},
            payload_schema_version=1,
            idempotency_key="k",
        ).cursor
        j.commit_state(AgentState.covering(scope, cursor, {"turn": 1}))

    with FileJournal(root) as j, pytest.MonkeyPatch.context() as patch:
        _break_writes_after(patch, calls=0)
        with pytest.raises(OSError):
            j.commit_state(AgentState.covering(scope, cursor, {"turn": 2}))

    with FileJournal(root) as j:
        assert j.recover(scope).state == AgentState.covering(scope, cursor, {"turn": 1})


# --------------------------------------------------------------------------
# 中间损坏
# --------------------------------------------------------------------------


def test_corruption_in_the_middle_raises_instead_of_skipping(tmp_path: Path, scope: Scope) -> None:
    root = tmp_path / "journal"
    _write(root, 5, scope)
    path = _events_path(root, scope)
    lines = path.read_bytes().splitlines(keepends=True)
    lines[2] = b"{not json at all}\n"
    path.write_bytes(b"".join(lines))

    # 静默跳过等于丢数据：第 3 条没了但调用方毫不知情。
    with pytest.raises(CorruptRecordError), FileJournal(root) as j:
        j.recover(scope)


def test_record_from_another_scope_is_rejected(tmp_path: Path, scope: Scope) -> None:
    """记录里的 scope 与文件路径不一致 = 越权读，必须挡住。"""
    root = tmp_path / "journal"
    _write(root, 3, scope)
    path = _events_path(root, scope)
    data = path.read_bytes().replace(b'"session_id":"s1"', b'"session_id":"s2"')
    path.write_bytes(data)

    with pytest.raises(CorruptRecordError), FileJournal(root) as j:
        j.recover(scope)


def _raw_event(scope: Scope, **overrides: object) -> bytes:
    record: dict[str, object] = {
        "user_id": scope.user_id,
        "session_id": scope.session_id,
        "type": EventType.USER_MESSAGE,
        "payload": {"content": "x"},
        "payload_schema_version": 1,
        "idempotency_key": "k",
        "occur_dt": None,
        "commit_dt": "2026-10-14T00:00:00.000000Z",
    }
    record.update(overrides)
    return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")


def test_an_unknown_payload_schema_version_is_rejected(tmp_path: Path, scope: Scope) -> None:
    """读出来时也要认版本。写的时候判过不够 —— 日志可能被别的写者升级过，也可能
    是手改的。放行等于让一条「当时不认」的记录照常投影出去。"""
    root = tmp_path / "journal"
    path = _events_path(root, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_raw_event(scope, payload_schema_version=2))

    with pytest.raises(CorruptRecordError), FileJournal(root) as j:
        j.recover(scope)


def test_a_payload_that_does_not_match_its_schema_is_rejected(tmp_path: Path, scope: Scope) -> None:
    root = tmp_path / "journal"
    path = _events_path(root, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_raw_event(scope, payload={"content": "x", "多出来的字段": 1}))

    with pytest.raises(CorruptRecordError), FileJournal(root) as j:
        j.recover(scope)


def test_a_payload_missing_a_required_field_is_rejected(tmp_path: Path, scope: Scope) -> None:
    root = tmp_path / "journal"
    path = _events_path(root, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_raw_event(scope, payload={}))

    with pytest.raises(CorruptRecordError), FileJournal(root) as j:
        j.recover(scope)


def test_an_unknown_state_schema_version_is_rejected(tmp_path: Path, scope: Scope) -> None:
    root = tmp_path / "journal"
    with FileJournal(root) as j:
        cursor = j.append(
            scope,
            EventType.USER_MESSAGE,
            {"content": "x"},
            payload_schema_version=1,
            idempotency_key="k",
        ).cursor
        j.commit_state(AgentState.covering(scope, cursor, {"turn": 1}))

    path = _state_path(root, scope)
    lines = path.read_bytes().splitlines(keepends=True)
    record = json.loads(lines[0])
    record["payload_schema_version"] = 9
    path.write_bytes((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))

    with pytest.raises(CorruptRecordError), FileJournal(root) as j:
        j.recover(scope)


# --------------------------------------------------------------------------
# 路径安全
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../../etc", "..", "/tmp/abs", "a/b", ""])
def test_traversal_id_cannot_become_a_scope(bad: str) -> None:
    """路径穿越的防线在 `Scope` 构造，不在文件层。

    拿不到合法的 `Scope` 就进不了 journal，坏 id 根本到不了拼路径那一步 ——
    比「拼完再检查路径有没有跑出 root」更早，也更不容易漏。
    """

    with pytest.raises(ScopeError):
        Scope(user_id=bad, session_id="s1")
    with pytest.raises(ScopeError):
        Scope(user_id="u1", session_id=bad)


def test_scope_validation_is_what_prevents_escaping_root(tmp_path: Path) -> None:
    """证明这道防线是必要的：没有它，拼接确实会跑出 root。"""

    root = (tmp_path / "journal").resolve()
    assert root not in (root / "../../etc" / "s1").resolve().parents
    with pytest.raises(ScopeError):
        Scope(user_id="../../etc", session_id="s1")
