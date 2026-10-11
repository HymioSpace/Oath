"""Journal —— 日志存储，`spec/feature07-journal.md` 的实现。

    records.py      值对象
    capabilities.py 能力名，adapter 与 conformance 套件共用
    validation.py   校验
    cursor.py       分页位置
    jsonl.py        落盘实现，Oath 的默认存储

这里没有单独的接口声明文件：`JsonlJournal` 的方法就是接口，约束由
`validate_batch` 和 conformance 套件把关。等出现第二个实现、调用方要同时面向两者时
再抽。
"""
from __future__ import annotations

from core.journal.capabilities import BLOB_STORE, MULTI_PROCESS, REOPEN, SIMULATE_CRASH
from core.journal.cursor import Cursor, CursorCodec, check_page_limit, filter_digest
from core.journal.jsonl import FileBlobStore, JsonlJournal, JsonlLease
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
    # adapter
    "FileBlobStore",
    "JsonlJournal",
    "JsonlLease",
]
