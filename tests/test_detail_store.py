"""detail store：按 scope 隔离，key 受限，取不到就说取不到。"""

from __future__ import annotations

from pathlib import Path

from core.detail.impl import FileDetailStore, is_valid_detail_key
from core.protocol.datatype import Scope


def test_round_trips_the_whole_text(tmp_path: Path) -> None:
    store = FileDetailStore(tmp_path / "details")
    scope = Scope("u1", "s1")
    content = "第一行\n第二行\n" * 500

    store.put(scope, "inv-1", content)

    assert store.get(scope, "inv-1") == content


def test_another_scope_never_sees_it(tmp_path: Path) -> None:
    store = FileDetailStore(tmp_path / "details")
    store.put(Scope("u1", "s1"), "inv-1", "内容")

    assert store.get(Scope("u2", "s1"), "inv-1") is None
    assert store.get(Scope("u1", "s2"), "inv-1") is None


def test_unknown_key_is_none(tmp_path: Path) -> None:
    store = FileDetailStore(tmp_path / "details")

    assert store.get(Scope("u1", "s1"), "nothing") is None


def test_keys_that_could_escape_the_scope_directory_are_refused(tmp_path: Path) -> None:
    store = FileDetailStore(tmp_path / "details")
    scope = Scope("u1", "s1")

    for key in ("../other", "a/b", "", ".hidden", "x" * 129):
        assert not is_valid_detail_key(key)
        assert store.get(scope, key) is None
    try:
        store.put(scope, "../escape", "内容")
    except ValueError:
        pass
    else:
        raise AssertionError("越界的 key 必须拒绝")

    assert list((tmp_path / "details").parent.glob("escape*")) == []
