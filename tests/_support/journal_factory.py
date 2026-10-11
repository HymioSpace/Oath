"""Adapter 工厂注册表 —— conformance 套件唯一的扩展点。

加一个 adapter 就是在这里加一条，不用改任何测试。

工厂交出 `Harness`（journal + 租约 + blob store）而不是裸 journal：fencing 检查要租约，
payload_ref 检查要 blob store。做成必需项，没提供就直接跑不了，不会悄悄跳过。
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from core.clock import ManualClock
from core.journal import (
    FileBlobStore,
    JournalLimits,
    JsonlJournal,
    JsonlLease,
    Principal,
    Scope,
)


@dataclass(frozen=True, slots=True)
class Harness:
    """一个被测 adapter 的全部外部可见部件。"""

    journal: JsonlJournal
    lease: JsonlLease
    blobs: FileBlobStore


def _jsonl(tmp_path: Any) -> Iterator[Harness]:
    journal = JsonlJournal(root=tmp_path, clock=ManualClock(), limits=JournalLimits())
    try:
        yield Harness(
            journal=journal,
            lease=journal.lease(),
            blobs=journal.blobs,
        )
    finally:
        journal.close()


ADAPTERS: dict[str, Any] = {
    "jsonl": _jsonl,
}


def make_scope(user: str = "u1", session: str = "s1") -> Scope:
    return Scope(user_id=user, session_id=session)


def make_principal(
    principal_id: str = "p1",
    *,
    user: str = "u1",
    session: str = "s1",
    unrestricted: bool = False,
) -> Principal:
    """构造测试用 principal。

    默认**只授权 (u1, s1)**，不是 unrestricted —— 隔离测试必须从一个真的受限主体
    开始，否则「越权被拒」这条断言在默认配置下就是空的。
    """
    if unrestricted:
        return Principal.unrestricted(principal_id)
    return Principal(principal_id=principal_id, scopes=frozenset({(user, session)}))