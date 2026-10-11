"""记录校验 —— 结构、部署限制、内容可编码性。

先判可编码性再算大小：`_record_size` 内部要序列化 payload，payload 不可编码时
它会先抛 TypeError，同批其他记录的问题就报不出来了。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from core.codec import canonical_bytes, hash_json
from core.journal.records import JournalLimits, RecordDraft
from core.primitives import QUEUE_NAMES


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """一条校验失败。`index` 是批次内的下标，便于一次报出全部问题。"""

    index: int
    code: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.index}] {self.code}: {self.detail}"


def _record_size(draft: RecordDraft) -> int:
    """单条记录的近似字节数。用 payload_ref 时只算引用本身。"""
    payload = draft.payload
    if payload is None:
        return 128  # 固定字段的保守估计
    return len(canonical_bytes(payload)) + 128


def validate_batch(
    drafts: Sequence[RecordDraft],
    limits: JournalLimits,
    *,
    allowed_types: frozenset[str] | None = None,
    allowed_categories: frozenset[str] | None = None,
    require_state: bool = False,
) -> list[ValidationIssue]:
    """校验整批，返回全部问题；空列表表示通过。

    一批里任一条不合法就整批拒绝。所以这里收集全部问题，而不是遇到第一个就抛。
    """
    issues: list[ValidationIssue] = []

    if not drafts:
        issues.append(ValidationIssue(-1, "empty_batch", "记录列表为空"))
        return issues

    if len(drafts) > limits.max_batch_records:
        issues.append(
            ValidationIssue(
                -1,
                "batch_too_large",
                f"批次 {len(drafts)} 条超过上限 {limits.max_batch_records}",
            )
        )

    seen_record_ids: dict[str, int] = {}
    for index, draft in enumerate(drafts):
        if draft.category == "state" and not require_state:
            issues.append(
                ValidationIssue(
                    index,
                    "state_via_append",
                    "category=state 只能通过 commitState 写入，不能走 append",
                )
            )

        if allowed_types is not None and draft.type not in allowed_types:
            issues.append(
                ValidationIssue(
                    index,
                    "type_not_allowed",
                    f"类型 {draft.type!r} 不在 writer capability 白名单内",
                )
            )

        if allowed_categories is not None and draft.category not in allowed_categories:
            issues.append(
                ValidationIssue(
                    index,
                    "category_not_allowed",
                    f"分类 {draft.category!r} 不在 writer capability 白名单内",
                )
            )

        if draft.queue is not None and draft.queue not in QUEUE_NAMES:
            issues.append(
                ValidationIssue(index, "unknown_queue", f"未知队列 {draft.queue!r}")
            )

        # 批内 record_id 重复会导致存储端唯一索引冲突，且报错信息难懂，提前拦。
        if draft.record_id in seen_record_ids:
            issues.append(
                ValidationIssue(
                    index,
                    "duplicate_record_id_in_batch",
                    f"record_id {draft.record_id!r} 与第 {seen_record_ids[draft.record_id]} 条重复",
                )
            )
        else:
            seen_record_ids[draft.record_id] = index

        # 先判可编码性再算大小，理由见模块 docstring。
        encodable = True
        if draft.payload is not None:
            try:
                canonical_bytes(draft.payload)
            except TypeError as exc:
                encodable = False
                issues.append(ValidationIssue(index, "payload_not_encodable", str(exc)))

        if encodable:
            size = _record_size(draft)
            if size > limits.max_record_bytes:
                issues.append(
                    ValidationIssue(
                        index,
                        "record_too_large",
                        f"记录约 {size} 字节，超过上限 {limits.max_record_bytes}",
                    )
                )

            # 需要走 blob 的场景：event 类记录超阈值时必须显式选择 blob 存储。
            if (
                draft.category == "event"
                and draft.payload is not None
                and size > limits.max_event_bytes
            ):
                issues.append(
                    ValidationIssue(
                        index,
                        "event_payload_too_large",
                        f"事件 payload 约 {size} 字节超过 {limits.max_event_bytes}，"
                        f"请改用 payload_ref 存入 blob",
                    )
                )

    return issues


def payload_hash_of(draft: RecordDraft) -> str:
    """记录的 payload_hash。

    生产者可以显式提供（大 payload 场景，内容不必再传一遍），否则从 payload 算。
    blob 引用记录的是 blob 内容本身的 hash，不是引用结构的 hash ——
    这样同一份内容走 payload 还是 blob，去重判定都一样。
    """
    if draft.payload_hash is not None:
        return draft.payload_hash
    if draft.payload is not None:
        return hash_json(dict(draft.payload))
    if draft.payload_ref is not None:
        return draft.payload_ref.hash
    return hash_json({})