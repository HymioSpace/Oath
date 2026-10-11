"""Journal —— 日志存储，`spec/feature07-journal.md` 的实现。

    api.py          接口：Journal / Writer / StateWriter / Lease / BlobStore + 能力名
    records.py      值对象
    cursor.py       分页位置
    validation.py   校验
    jsonl.py        api.py 的一种实现，Oath 的默认存储
"""
from __future__ import annotations

from core.journal.api import (
    BLOB_STORE,
    MULTI_PROCESS,
    REOPEN,
    SIMULATE_CRASH,
    BlobStore,
    Journal,
    Lease,
    StateWriter,
    Writer,
)
from core.journal.cursor import Cursor, CursorCodec, check_page_limit, filter_digest
from core.journal.jsonl import (
    FileBlobStore,
    JsonlJournal,
    JsonlLease,
    JsonlStateWriter,
    JsonlWriter,
)
from core.journal.records import (
    BlobRef,
    CommitBatch,
    CommitReceipt,
    Head,
    JournalLimits,
    JournalRecord,
    Principal,
    ReadPage,
    ReadRequest,
    RecordDraft,
    RecordFilter,
    Scope,
)
from core.journal.validation import ValidationIssue, payload_hash_of, validate_batch

__all__ = [
    # 接口
    "BlobStore",
    "Journal",
    "Lease",
    "StateWriter",
    "Writer",
    # 值对象
    "BlobRef",
    "CommitBatch",
    "CommitReceipt",
    "Head",
    "JournalLimits",
    "JournalRecord",
    "Principal",
    "ReadPage",
    "ReadRequest",
    "RecordDraft",
    "RecordFilter",
    "Scope",
    # 能力名
    "BLOB_STORE",
    "MULTI_PROCESS",
    "REOPEN",
    "SIMULATE_CRASH",
    # 校验
    "ValidationIssue",
    "payload_hash_of",
    "validate_batch",
    # cursor
    "Cursor",
    "CursorCodec",
    "check_page_limit",
    "filter_digest",
    # 实现
    "FileBlobStore",
    "JsonlJournal",
    "JsonlLease",
    "JsonlStateWriter",
    "JsonlWriter",
]
