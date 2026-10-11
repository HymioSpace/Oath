"""Cursor —— 分页位置。

两件事：
1. cursor 记住**已扫描到的 seq**，包括被过滤掉的记录。否则一整页都被过滤时，
   客户端拿到空页会以为流结束了。
2. cursor 绑定 principal、scope、过滤条件和 limit，换任何一个都不能继续用。

cursor 是位置，不是凭据 —— 它里面只有 seq，能读到什么由每次调用携带的
`Principal` 决定。所以这里不做签名：调用方本来就能直接指定 `after_seq`，
签名拦不住任何东西。等接口变成「客户端只能拿 cursor 续读」时再加。
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.codec import canonical_bytes, from_iso, hash_json, to_iso
from core.errors import CursorInvalid, LimitExceeded
from core.journal.records import ReadRequest, RecordFilter


def filter_digest(record_filter: RecordFilter) -> str:
    """过滤条件的哈希。换了过滤条件，cursor 就不能继续用。"""
    return hash_json(record_filter.digest_parts())[:32]


@dataclass(frozen=True, slots=True)
class Cursor:
    """分页位置。

    `after_seq` 是本次请求的起点，`through_seq` 是本次扫描到的位置（也即下一页的起点）。
    两者分开是为了处理「扫描过但被过滤掉」的记录：空页也可能带着非零的 through_seq。
    """

    principal_id: str
    authority_id: str
    user_id: str
    session_id: str
    after_seq: int
    through_seq: int
    limit: int
    filter_digest: str
    issued_at: datetime

    @classmethod
    def from_request(
        cls,
        request: ReadRequest,
        *,
        through_seq: int,
        issued_at: datetime,
    ) -> Cursor:
        return cls(
            principal_id=request.principal.principal_id,
            authority_id=request.principal.authority_id,
            user_id=request.scope.user_id,
            session_id=request.scope.session_id,
            after_seq=request.after_seq,
            through_seq=through_seq,
            limit=request.limit,
            filter_digest=filter_digest(request.filter),
            issued_at=issued_at,
        )

    def validate_for(self, request: ReadRequest) -> None:
        """校验这个 cursor 能否用于当前请求，任何一项不匹配都抛 CursorInvalid。

        不做「尽力而为」的放宽：换个位置继续读会把事件漏掉，而且没人会发现。
        """
        mismatches: list[str] = []
        if self.principal_id != request.principal.principal_id:
            mismatches.append("principal 不匹配")
        if self.authority_id != request.principal.authority_id:
            mismatches.append("authority 不匹配")
        if self.user_id != request.scope.user_id:
            mismatches.append("user_id 不匹配")
        if self.session_id != request.scope.session_id:
            mismatches.append("session_id 不匹配")
        if self.filter_digest != filter_digest(request.filter):
            mismatches.append("过滤条件不匹配")
        if self.limit != request.limit:
            mismatches.append("limit 不匹配")
        if mismatches:
            raise CursorInvalid("; ".join(mismatches))

    def to_json(self) -> dict[str, Any]:
        return {
            "p": self.principal_id,
            "a": self.authority_id,
            "u": self.user_id,
            "s": self.session_id,
            "f": self.after_seq,
            "t": self.through_seq,
            "n": self.limit,
            "d": self.filter_digest,
            "i": to_iso(self.issued_at),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Cursor:
        try:
            return cls(
                principal_id=raw["p"],
                authority_id=raw["a"],
                user_id=raw["u"],
                session_id=raw["s"],
                after_seq=int(raw["f"]),
                through_seq=int(raw["t"]),
                limit=int(raw["n"]),
                filter_digest=raw["d"],
                issued_at=from_iso(raw["i"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CursorInvalid(f"cursor 结构非法: {exc}") from exc


class CursorCodec:
    """cursor 的编解码：base64 + JSON。

    合法性由 `Cursor.validate_for` 判断。
    """

    def seal(self, cursor: Cursor) -> str:
        return _b64(canonical_bytes(cursor.to_json()))

    def unseal(self, token: str, request: ReadRequest) -> Cursor:
        """解出 cursor 并校验它适用于当前请求。出错一律抛 CursorInvalid。"""
        try:
            body = _unb64(token)
        except Exception as exc:
            raise CursorInvalid("cursor 编码非法") from exc

        try:
            raw = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CursorInvalid("cursor 内容不是合法 JSON") from exc

        cursor = Cursor.from_json(raw)
        cursor.validate_for(request)
        return cursor


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(token: str) -> bytes:
    padding = "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(token + padding)


def check_page_limit(limit: int, max_page_size: int) -> None:
    """页大小上限，默认 100。超限是配置错误。"""
    if limit < 1:
        raise LimitExceeded(f"limit 必须 >= 1，实际 {limit}")
    if limit > max_page_size:
        raise LimitExceeded(f"limit {limit} 超过最大页大小 {max_page_size}")