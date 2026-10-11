"""Journal 的记录与查询值对象。

两个记录类型，区别在谁赋值：

- `RecordDraft`  —— 生产者提交的，含 payload，不含 seq / committed_at。
- `JournalRecord` —— 存储端返回的，`seq` 与 `committed_at` 由 Journal 分配。

checksum 只覆盖 RecordDraft 的内容（见 core/codec.py `checksum_of`）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.codec import from_iso, to_iso
from core.primitives import (
    SCHEMA_VERSION,
    AppendOutcome,
    QueueName,
    RecordCategory,
)

# ---------- 身份与隔离 ----------

@dataclass(frozen=True, slots=True)
class Scope:
    """`(user_id, session_id)` —— journal 的隔离边界，也是存储键的一部分。

    跨用户读不到，是因为它们根本不在同一个日志流里，不是靠查询时过滤。
    """

    user_id: str
    session_id: str

    def __post_init__(self) -> None:
        if not self.user_id:
            raise ValueError("Scope.user_id 不能为空")
        if not self.session_id:
            raise ValueError("Scope.session_id 不能为空")

    def key(self) -> tuple[str, str]:
        """存储键。JSONL 用它定位日志目录，SQLite 用它做复合主键。"""
        return (self.user_id, self.session_id)


@dataclass(frozen=True, slots=True)
class Principal:
    """认证主体。

    `authority_id` 记录是谁认证的。v0.1 只有一个 authority，多租户留到以后。
    """

    principal_id: str
    authority_id: str = "default"
    scopes: frozenset[tuple[str, str]] = frozenset()

    def __post_init__(self) -> None:
        if not self.principal_id:
            raise ValueError("Principal.principal_id 不能为空")

    def can_access(self, scope: Scope) -> bool:
        """空 scopes 表示不限制（单用户部署）。非空则是显式白名单。"""
        if not self.scopes:
            return True
        return scope.key() in self.scopes

    @classmethod
    def unrestricted(cls, principal_id: str, authority_id: str = "default") -> Principal:
        """单用户部署：可以访问任意 scope。scope 检查仍会在 journal 层执行。"""
        return cls(principal_id=principal_id, authority_id=authority_id, scopes=frozenset())


# ---------- blob 引用 ----------

@dataclass(frozen=True, slots=True)
class BlobRef:
    """超大 payload 的不可变引用。

    blob 必须先落盘再提交引用。hash 用来验证 blob 没被换掉，size 用来判断
    拉取是否超预算。
    """

    blob_id: str
    hash: str
    size: int

    def __post_init__(self) -> None:
        if self.size < 0:
            raise ValueError("BlobRef.size 不能为负")


# ---------- 记录 ----------

@dataclass(frozen=True, slots=True)
class RecordDraft:
    """生产者提交的一条记录，存储端字段尚未赋值。

    `event_id` 单独放字段而不是塞进 payload：去重键是
    `(user_id, session_id, event_id)`，放 payload 里就得先把 payload 反序列化才能比对。
    只有 category="event" 必填。
    """

    record_id: str
    type: str
    category: RecordCategory
    producer: str
    payload: Mapping[str, Any] | None = None
    payload_ref: BlobRef | None = None
    run_id: str | None = None
    step_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    occurred_at: datetime | None = None
    schema_version: int = SCHEMA_VERSION
    queue: QueueName | None = None
    event_id: str | None = None
    #: 去重用的内容哈希。留空则由 Journal 从内容计算（见 jsonl.py）。
    payload_hash: str | None = None

    def __post_init__(self) -> None:
        if not self.record_id:
            raise ValueError("RecordDraft.record_id 不能为空")
        if not self.type:
            raise ValueError("RecordDraft.type 不能为空")
        if not self.producer:
            raise ValueError("RecordDraft.producer 不能为空")
        if self.category not in ("event", "state", "observation"):
            raise ValueError(f"未知 category: {self.category}")
        if self.payload is not None and self.payload_ref is not None:
            raise ValueError("payload 与 payload_ref 不能同时提供")
        if self.category == "event" and not self.queue:
            raise ValueError("category=event 的记录必须指定 queue")
        if self.category != "event" and self.queue is not None:
            raise ValueError(f"只有 category=event 能带 queue，实际 category={self.category}")
        if self.category == "event" and not self.event_id:
            raise ValueError("category=event 的记录必须带 event_id（feature01 去重键）")

    def content_parts(self) -> dict[str, Any]:
        """参与 checksum / payload_hash 的内容。

        不含 seq 和 committed_at —— 这两个是存储端赋的，生产者重试时算不出来。
        `occurred_at` 要算进去：它是生产者声称的时间，重试必须是同一个值。
        """
        parts: dict[str, Any] = {
            "record_id": self.record_id,
            "type": self.type,
            "category": self.category,
            "producer": self.producer,
            "schema_version": self.schema_version,
        }
        if self.payload is not None:
            parts["payload"] = dict(self.payload)
        if self.payload_ref is not None:
            parts["payload_ref"] = {
                "blob_id": self.payload_ref.blob_id,
                "hash": self.payload_ref.hash,
                "size": self.payload_ref.size,
            }
        for name in ("run_id", "step_id", "correlation_id", "causation_id", "queue", "event_id"):
            value = getattr(self, name)
            if value is not None:
                parts[name] = value
        if self.occurred_at is not None:
            parts["occurred_at"] = to_iso(self.occurred_at)
        return parts


@dataclass(frozen=True, slots=True)
class JournalRecord:
    """存储端返回的记录。`seq` 与 `committed_at` 由 Journal 分配。"""

    seq: int
    record_id: str
    scope: Scope
    commit_id: str
    type: str
    category: RecordCategory
    producer: str
    payload_hash: str
    occurred_at: datetime
    committed_at: datetime
    schema_version: int = SCHEMA_VERSION
    payload: Mapping[str, Any] | None = None
    payload_ref: BlobRef | None = None
    run_id: str | None = None
    step_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    queue: QueueName | None = None
    event_id: str | None = None

    def __post_init__(self) -> None:
        if self.seq < 1:
            raise ValueError(f"JournalRecord.seq 必须 >= 1，实际 {self.seq}")

    @property
    def committed_at_iso(self) -> str:
        return to_iso(self.committed_at)

    def to_row(self) -> dict[str, Any]:
        """落盘用的扁平 dict。全部显式列出，不用 dataclasses.asdict ——
        扁平结构落盘后能直接读回来，不需要额外转换。"""
        return {
            "seq": self.seq,
            "record_id": self.record_id,
            "commit_id": self.commit_id,
            "type": self.type,
            "category": self.category,
            "producer": self.producer,
            "schema_version": self.schema_version,
            "payload": dict(self.payload) if self.payload is not None else None,
            "payload_ref": (
                {
                    "blob_id": self.payload_ref.blob_id,
                    "hash": self.payload_ref.hash,
                    "size": self.payload_ref.size,
                }
                if self.payload_ref is not None
                else None
            ),
            "run_id": self.run_id,
            "step_id": self.step_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "queue": self.queue,
            "event_id": self.event_id,
            "occurred_at": to_iso(self.occurred_at),
            "committed_at": to_iso(self.committed_at),
            "payload_hash": self.payload_hash,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any], scope: Scope) -> JournalRecord:
        ref = row.get("payload_ref")
        return cls(
            seq=row["seq"],
            record_id=row["record_id"],
            scope=scope,
            commit_id=row["commit_id"],
            type=row["type"],
            category=row["category"],
            producer=row["producer"],
            payload_hash=row["payload_hash"],
            occurred_at=from_iso(row["occurred_at"]),
            committed_at=from_iso(row["committed_at"]),
            schema_version=row["schema_version"],
            payload=row.get("payload"),
            payload_ref=BlobRef(**ref) if ref else None,
            run_id=row.get("run_id"),
            step_id=row.get("step_id"),
            correlation_id=row.get("correlation_id"),
            causation_id=row.get("causation_id"),
            queue=row.get("queue"),
            event_id=row.get("event_id"),
        )


# ---------- 提交 ----------

@dataclass(frozen=True, slots=True)
class CommitBatch:
    """一次原子提交。`records` 要么全部可见，要么全部不可见。"""

    commit_id: str
    scope: Scope
    records: tuple[RecordDraft, ...]

    def __post_init__(self) -> None:
        if not self.commit_id:
            raise ValueError("CommitBatch.commit_id 不能为空")
        if not self.records:
            raise ValueError("CommitBatch 不能提交空记录列表")

    def content_parts(self) -> dict[str, Any]:
        """commit checksum 的内容。按记录顺序参与 —— 顺序影响 seq 分配，因此影响内容。"""
        return {
            "commit_id": self.commit_id,
            "records": [record.content_parts() for record in self.records],
        }


@dataclass(frozen=True, slots=True)
class CommitReceipt:
    """提交回执。落盘成功之后才返回。

    receipt 丢了就用原 commit_id 重试，不重新生成 record ID —— 会拿回内容相同的
    旧 receipt，outcome 是 "duplicate"。
    """

    outcome: AppendOutcome
    commit_id: str
    scope: Scope
    first_seq: int | None = None
    record_count: int = 0
    checksum: str | None = None
    committed_at: datetime | None = None
    state_version: int | None = None
    owner_epoch: int | None = None
    detail: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome in ("accepted", "duplicate")


# ---------- 读取 ----------

@dataclass(frozen=True, slots=True)
class RecordFilter:
    """读取过滤条件。全部为空的 filter 匹配所有记录。`queues` 只对 event 记录有意义。"""

    categories: frozenset[str] = frozenset()
    types: frozenset[str] = frozenset()
    queues: frozenset[str] = frozenset()
    run_ids: frozenset[str] = frozenset()
    producers: frozenset[str] = frozenset()

    def matches(self, record: JournalRecord) -> bool:
        # 逐条 early-return 而不是几个条件的合取：读路径上每条记录都要过这个函数，
        # 而五个条件的优先级顺序（category → type → queue → run → producer）本身
        # 是有意义的 —— 先排除最不可能命中的维度。
        if self.categories and record.category not in self.categories:
            return False
        if self.types and record.type not in self.types:
            return False
        if self.queues and (record.queue is None or record.queue not in self.queues):
            return False
        if self.run_ids and (record.run_id is None or record.run_id not in self.run_ids):
            return False
        return not (self.producers and record.producer not in self.producers)

    def digest_parts(self) -> dict[str, Any]:
        """参与 cursor filter_digest 的内容。空集合与 None 语义不同，必须能区分。"""
        return {
            "categories": sorted(self.categories),
            "types": sorted(self.types),
            "queues": sorted(self.queues),
            "run_ids": sorted(self.run_ids),
            "producers": sorted(self.producers),
        }


@dataclass(frozen=True, slots=True)
class ReadRequest:
    principal: Principal
    scope: Scope
    after_seq: int = 0
    limit: int = 100
    filter: RecordFilter = field(default_factory=RecordFilter)


@dataclass(frozen=True, slots=True)
class ReadPage:
    """一页读取结果。

    `through_seq` 是**扫描到**的位置，不是最后一条命中记录的位置：被 filter 排除的
    记录同样推进它。否则客户端翻页拿到空页会以为流结束了，就此停止消费。
    """

    items: tuple[JournalRecord, ...]
    through_seq: int
    head_seq: int
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        if self.through_seq < 0:
            raise ValueError("ReadPage.through_seq 不能为负")
        if self.through_seq > self.head_seq:
            raise ValueError(
                f"ReadPage.through_seq({self.through_seq}) 不能大于 head_seq({self.head_seq})"
            )


@dataclass(frozen=True, slots=True)
class Head:
    """一个 scope 当前写到哪了：最新 seq、状态版本、owner epoch。"""

    seq: int
    state_version: int
    owner_epoch: int

    def __post_init__(self) -> None:
        if self.seq < 0 or self.state_version < 0 or self.owner_epoch < 0:
            raise ValueError("Head 的计数器不能为负")


# ---------- 部署限制 ----------

@dataclass(frozen=True, slots=True)
class JournalLimits:
    """部署施加的上限。超过上限要明确报错，不能默默丢掉（feature07）。"""

    max_record_bytes: int = 1 << 20  # 1 MiB
    max_batch_records: int = 256
    max_page_size: int = 100
    max_event_bytes: int = 256 << 10  # 256 KiB，超过则要求走 blob