"""超阈值结果截断：全文留给 detail store，发给模型的正文保头留尾。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.protocol.datatype import Scope
from core.protocol.interface.detail import DetailStore
from core.utils.encoding import JsonValue, canonical_json

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Detail:
    """被挪走的正文在哪、有多长。随终态落盘。"""

    key: str
    original_chars: int


def notice(detail: Detail) -> str:
    return (
        f"\n[中间 {detail.original_chars} 字已省略。需要完整内容请调用 detail_read 工具，"
        f"detail_key={detail.key}]\n"
    )


class ResultShrinker:
    """把过长的结果正文截短。`whole` 里的工具永远原样返回。"""

    def __init__(
        self, store: DetailStore, limit: int, *, whole: frozenset[str] = frozenset()
    ) -> None:
        if limit < 1:
            raise ValueError(f"截断阈值必须是正整数: {limit!r}")
        self._store, self._limit, self._whole = store, limit, whole

    def shrink(
        self, tool: str, scope: Scope, invocation_id: str, content: JsonValue
    ) -> tuple[JsonValue, Detail | None]:
        """返回 `(content, detail)`。

        写 detail 失败时返回原文和 `None`：宁可多发一段长文本，也不让 journal 里
        出现一个指向不存在内容的 key —— 那条记录一旦落盘就撤不回来。
        """
        if tool in self._whole:
            return content, None
        text = content if isinstance(content, str) else canonical_json(content)
        if len(text) <= self._limit:
            return content, None
        detail = Detail(key=invocation_id, original_chars=len(text))
        try:
            self._store.put(scope, detail.key, text)
        except Exception:
            _log.exception("detail 写入失败，保留完整结果 %s", invocation_id)
            return content, None
        head = self._limit // 2
        return f"{text[:head]}{notice(detail)}{text[-(self._limit - head) :]}", detail


__all__ = ["Detail", "ResultShrinker", "notice"]
