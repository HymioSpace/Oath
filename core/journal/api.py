"""Journal 的接口 —— 一种存储实现必须提供哪些方法。

    api.py         本文件，接口与能力名
    records.py     值对象
    cursor.py      分页位置
    validation.py  校验
    jsonl.py       JSONL 实现

实现类显式继承对应的 Protocol（`class JsonlJournal(Journal)`），mypy 逐个核对
签名 —— 不靠「方法名碰巧对上」。换 SQLite 实现时，要满足的是这里，不是 jsonl.py。

**两类东西不在接口里，它们是能力**：`reopen()`（REOPEN）和
`crash_on_next_commit()`（SIMULATE_CRASH）。不是每种存储都做得到，所以调用方先看
`capabilities` 再决定要不要用。构造参数同样不在接口里 —— `JsonlJournal(root=...)`
是 JSONL 自己的事。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Protocol

from core.journal.cursor import Cursor
from core.journal.records import (
    CommitReceipt,
    Head,
    JournalLimits,
    Principal,
    ReadPage,
    ReadRequest,
    RecordDraft,
    Scope,
)

# ---------- 能力名 ----------
#
# 实现通过 `capabilities` 声明自己支持哪些，conformance 套件据此决定跑还是跳过
# 对应检查。少声明一项不会让测试失败，只会让它跳过 —— 所以声明要和实现一致。

#: 关闭后重新打开能读到全部已提交内容。
REOPEN: Final = "reopen"
#: 支持在提交中途注入崩溃（测试钩子）。
SIMULATE_CRASH: Final = "simulate_crash"
#: 同一份日志被多个进程同时写仍然正确。
MULTI_PROCESS: Final = "multi_process"
#: 自带不可变 blob 存储。
BLOB_STORE: Final = "blob_store"


class BlobStore(Protocol):
    """不可变的大 payload 存储。内容寻址：同样的内容必然同一个 id。"""

    async def put(self, data: bytes) -> tuple[str, int]:
        """写入并返回 `(blob_id, size)`。同内容重复写入不报错。"""
        ...

    async def get(self, blob_id: str) -> bytes:
        """读回内容。不存在抛 KeyError。"""
        ...

    async def verify(self, blob_id: str, expected_hash: str) -> bool:
        """确认 blob 内容与声明的 hash 一致。"""
        ...


class Writer(Protocol):
    """普通模块的追加能力。绑定后 scope、producer、白名单就固定了。"""

    @property
    def scope(self) -> Scope: ...

    @property
    def producer(self) -> str: ...

    async def append(self, records: Sequence[RecordDraft], commit_id: str) -> CommitReceipt:
        """原子追加一批记录。落盘成功后才返回 receipt。"""
        ...


class StateWriter(Protocol):
    """执行状态提交能力。owner_epoch 在绑定时固定 —— writer 本身就是凭证。"""

    @property
    def scope(self) -> Scope: ...

    async def commit_state(
        self,
        records: Sequence[RecordDraft],
        commit_id: str,
        *,
        expected_state_version: int,
        owner_epoch: int,
    ) -> CommitReceipt:
        """提交一次状态迁移。epoch 或版本不符时不写入。"""
        ...


class Lease(Protocol):
    """owner 租约。epoch 递增，重启后仍单调。"""

    async def acquire(self, scope: Scope, holder: str) -> int:
        """拿租约，返回新的 owner_epoch。"""
        ...

    async def renew(self, scope: Scope, owner_epoch: int) -> bool:
        """确认自己还是 owner。"""
        ...


class Journal(Protocol):
    """日志存储。所有状态的唯一来源。"""

    @property
    def capabilities(self) -> frozenset[str]: ...

    @property
    def limits(self) -> JournalLimits: ...

    @property
    def blobs(self) -> BlobStore: ...

    def lease(self) -> Lease: ...

    def bind(
        self,
        principal: Principal,
        scope: Scope,
        producer: str,
        *,
        allowed_types: frozenset[str] | None = None,
        allowed_categories: frozenset[str] | None = None,
    ) -> Writer:
        """绑定一个 writer。越权或白名单不合法时抛异常。"""
        ...

    def bind_state(
        self,
        principal: Principal,
        scope: Scope,
        producer: str,
        *,
        owner_epoch: int,
        allowed_types: frozenset[str] | None = None,
    ) -> StateWriter:
        """绑定一个 state writer，固定在当前 epoch 上。"""
        ...

    async def read(self, request: ReadRequest) -> ReadPage: ...

    async def head(self, scope: Scope) -> Head: ...

    def unseal_cursor(self, token: str, request: ReadRequest) -> Cursor:
        """解出 cursor 并校验它适用于当前请求。"""
        ...

    def close(self) -> None: ...
