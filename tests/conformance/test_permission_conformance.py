"""`PermissionStore` 契约测试。

这些测试跑在 `PermissionStore` **接口**上，只描述接口承诺的语义（当前状态、
裁定只生效一次、跨 scope 查不到），不碰实现细节（文件路径、fd、内存索引）。
将来换实现（MySQL），同一组测试应该原样跑通 —— 那时把它们参数化到多个实现上，
而不是复制一份。

对应 `spec/feature-tool-system.md` 的「验收与验证」。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from core.protocol.datatype.permission import (
    PermissionRecord,
    PermissionStatus,
)
from core.protocol.datatype.scope import Scope
from core.protocol.error import PermissionStoreError
from core.tool.permission import PermissionStore
from core.tool.permission.impl import FilePermissionStore

SCOPE = Scope(user_id="u1", session_id="s1")
OTHER_SCOPE = Scope(user_id="u1", session_id="s2")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[PermissionStore]:
    """v1 只有 JSONL 一种实现。加第二个实现时把这个 fixture 参数化，不要复制测试。"""
    instance = FilePermissionStore(tmp_path)
    yield instance
    instance.close()


def request_record(
    invocation_id: str = "inv-1",
    *,
    scope: Scope = SCOPE,
    tool: str = "echo",
    tool_call_id: str = "tc-1",
) -> PermissionRecord:
    return PermissionRecord(
        scope=scope,
        tool_call_id=tool_call_id,
        invocation_id=invocation_id,
        tool=tool,
        arguments={},
        status=PermissionStatus.PENDING,
        requested_dt="2026-01-01T00:00:00.000000Z",
        expires_dt="2026-01-01T00:05:00.000000Z",
    )


def decided(record: PermissionRecord, status: str) -> PermissionRecord:
    assert status in PermissionStatus.RESOLVED
    return replace(
        record,
        status=status,
        decided_by="tester",
        decided_dt="2026-01-01T00:01:00.000000Z",
    )


# --------------------------------------------------------------------------
# 当前状态
# --------------------------------------------------------------------------


def test_a_new_request_is_pending(store: PermissionStore) -> None:
    store.record(request_record())

    got = store.get(SCOPE, "inv-1")
    assert got is not None
    assert got.status == PermissionStatus.PENDING
    assert not got.is_resolved


def test_an_unknown_request_reads_as_none(store: PermissionStore) -> None:
    """查询不是命令。查不到返回 `None`，不是异常。"""
    assert store.get(SCOPE, "never-asked") is None


def test_pending_lists_only_what_is_waiting(store: PermissionStore) -> None:
    store.record(request_record("inv-1"))
    store.record(request_record("inv-2", tool_call_id="tc-2"))
    store.record(request_record("inv-3", tool_call_id="tc-3"))
    store.resolve(decided(request_record("inv-2", tool_call_id="tc-2"), PermissionStatus.DENIED))

    assert [r.invocation_id for r in store.pending(SCOPE)] == ["inv-1", "inv-3"]


def test_a_recorded_request_cannot_be_recorded_again(store: PermissionStore) -> None:
    """同一个 `(scope, invocation_id)` 记两次 = 两条互相矛盾的审批。"""
    store.record(request_record())
    with pytest.raises(PermissionStoreError):
        store.record(request_record())


# --------------------------------------------------------------------------
# 裁定
# --------------------------------------------------------------------------


def test_a_verdict_takes_effect_once(store: PermissionStore) -> None:
    store.record(request_record())
    assert store.resolve(decided(request_record(), PermissionStatus.APPROVED)) is True

    got = store.get(SCOPE, "inv-1")
    assert got is not None
    assert got.status == PermissionStatus.APPROVED
    assert got.decided_by == "tester"


def test_a_second_verdict_is_dropped(store: PermissionStore) -> None:
    """两个审批人同时点「同意」：第二个人的点击必须无效。"""
    store.record(request_record())
    assert store.resolve(decided(request_record(), PermissionStatus.DENIED)) is True
    assert store.resolve(decided(request_record(), PermissionStatus.APPROVED)) is False

    got = store.get(SCOPE, "inv-1")
    assert got is not None
    assert got.status == PermissionStatus.DENIED


def test_verdicting_an_unknown_request_reports_failure(store: PermissionStore) -> None:
    assert store.resolve(decided(request_record(), PermissionStatus.APPROVED)) is False


def test_a_pending_status_cannot_be_written_as_a_verdict(store: PermissionStore) -> None:
    store.record(request_record())
    with pytest.raises(PermissionStoreError):
        store.resolve(request_record())


def test_verdicts_keep_their_own_order(store: PermissionStore) -> None:
    """被裁定掉的请求离开 `pending`，定完之后仍然查得到。"""
    store.record(request_record())
    store.resolve(decided(request_record(), PermissionStatus.EXPIRED))

    assert store.pending(SCOPE) == ()
    got = store.get(SCOPE, "inv-1")
    assert got is not None
    assert got.status == PermissionStatus.EXPIRED


# --------------------------------------------------------------------------
# scope 隔离
# --------------------------------------------------------------------------


def test_another_scope_cannot_see_the_record(store: PermissionStore) -> None:
    """用别的 session 去问得到 `None` —— 不泄露它存不存在。"""
    store.record(request_record())
    assert store.get(OTHER_SCOPE, "inv-1") is None
    assert store.pending(OTHER_SCOPE) == ()


def test_the_same_invocation_id_can_exist_in_two_scopes(store: PermissionStore) -> None:
    store.record(request_record())
    store.record(request_record(scope=OTHER_SCOPE))

    store.resolve(decided(request_record(), PermissionStatus.APPROVED))

    mine = store.get(SCOPE, "inv-1")
    theirs = store.get(OTHER_SCOPE, "inv-1")
    assert mine is not None and mine.status == PermissionStatus.APPROVED
    assert theirs is not None and theirs.status == PermissionStatus.PENDING
