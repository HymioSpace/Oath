"""超阈值结果截断：保头留尾、全文可取回、取不回时不截。"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.detail import DETAIL_READ, DetailReadTool, ResultShrinker
from core.detail.impl import FileDetailStore
from core.protocol.datatype import Scope, ToolInvocation
from core.protocol.error import ToolExecutionError
from core.protocol.interface.detail import DetailStore

SCOPE = Scope("u1", "s1")
LIMIT = 20


def shrinker(store: DetailStore, *, whole: frozenset[str] = frozenset()) -> ResultShrinker:
    return ResultShrinker(store, LIMIT, whole=whole)


@pytest.fixture
def store(tmp_path: Path) -> FileDetailStore:
    return FileDetailStore(tmp_path / "details")


def test_short_results_pass_through_untouched(store: FileDetailStore) -> None:
    content, detail = shrinker(store).shrink("echo", SCOPE, "inv-1", "短")

    assert content == "短"
    assert detail is None


def test_long_results_keep_head_and_tail(store: FileDetailStore) -> None:
    content, detail = shrinker(store).shrink("echo", SCOPE, "inv-1", "A" * 100 + "B" * 100)

    assert detail is not None
    text = str(content)
    assert text.startswith("A" * (LIMIT // 2))
    assert text.endswith("B" * (LIMIT - LIMIT // 2))
    assert "已省略" in text
    assert f"detail_key={detail.key}" in text
    assert text.count("A") == LIMIT // 2


def test_the_whole_text_is_what_gets_stored(store: FileDetailStore) -> None:
    whole = "A" * 100 + "B" * 100

    _, detail = shrinker(store).shrink("echo", SCOPE, "inv-1", whole)

    assert detail is not None
    assert store.get(SCOPE, "inv-1") == whole


def test_structured_results_become_truncated_text(store: FileDetailStore) -> None:
    content, detail = shrinker(store).shrink("echo", SCOPE, "inv-1", {"rows": ["x" * 200]})

    assert isinstance(content, str)
    assert detail is not None
    assert store.get(SCOPE, "inv-1") is not None


def test_the_detail_tool_is_never_shrunk(store: FileDetailStore) -> None:
    whole = "长" * 500

    content, detail = shrinker(store, whole=frozenset({DETAIL_READ})).shrink(
        DETAIL_READ, SCOPE, "inv-1", whole
    )

    assert content == whole
    assert detail is None
    assert store.get(SCOPE, "inv-1") is None


def test_a_failing_store_keeps_the_result_intact(tmp_path: Path) -> None:
    class Broken(DetailStore):
        def put(self, scope: Scope, key: str, content: str) -> None:
            raise OSError("磁盘满了（测试）")

        def get(self, scope: Scope, key: str) -> str | None:
            return None

    whole = "长" * 500

    content, detail = shrinker(Broken()).shrink("echo", SCOPE, "inv-1", whole)

    assert content == whole
    assert detail is None


def test_reading_gives_the_whole_text_back(store: FileDetailStore) -> None:
    whole = "完整内容" * 100
    store.put(SCOPE, "inv-1", whole)

    result = DetailReadTool(store).execute(
        ToolInvocation(SCOPE, "inv-2", {"detail_key": "inv-1"}, None)
    )

    assert result.content == whole


def test_reading_an_unknown_key_fails(store: FileDetailStore) -> None:
    with pytest.raises(ToolExecutionError):
        DetailReadTool(store).execute(ToolInvocation(SCOPE, "inv-2", {"detail_key": "nope"}, None))


def test_reading_another_scope_fails(store: FileDetailStore) -> None:
    store.put(SCOPE, "inv-1", "内容")

    with pytest.raises(ToolExecutionError):
        DetailReadTool(store).execute(
            ToolInvocation(Scope("u2", "s1"), "inv-2", {"detail_key": "inv-1"}, None)
        )
