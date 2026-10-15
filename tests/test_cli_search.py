"""`search` 这条命令本身：打分、排序、取几条。传输层在 `test_cli_server.py`。"""

from __future__ import annotations

from typing import Any, cast

from core.protocol.datatype.scope import Scope
from extensions.cli.search import search

SCOPE = Scope("u1", "s1")


def titles(query: str, limit: int = 3) -> list[str]:
    """按标题取命中项。返回值是 JSON，用例只关心这一层形状。"""
    body = cast(dict[str, Any], search(query, limit=limit, scope=SCOPE, user_context={}))
    return [hit["title"] for hit in cast(list[dict[str, Any]], body["hits"])]


def test_a_keyword_in_a_title_outranks_one_in_the_body() -> None:
    assert titles("凭证")[0] == "凭证"


def test_a_term_nobody_written_keeps_the_ones_that_were() -> None:
    """多个关键词按命中数累加，不要求全都出现 —— 部分匹配也有用。"""
    assert "工作区" in titles("工作区 不存在的词")


def test_nothing_written_is_no_hit() -> None:
    assert titles("量子纠缠") == []


def test_an_empty_query_searches_for_nothing() -> None:
    """空串切成空列表会让每条都算命中 —— 不如直接说没有。"""
    assert titles("   ") == []


def test_only_the_first_few_are_returned() -> None:
    assert titles("的", limit=2) == titles("的")[:2]
