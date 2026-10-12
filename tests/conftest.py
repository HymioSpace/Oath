"""测试共用夹具。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from core.journal.impl import FileJournal
from core.protocol.datatype import Scope

SCOPE = Scope(user_id="u1", session_id="s1")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "journal"


@pytest.fixture
def journal(root: Path) -> Iterator[FileJournal]:
    with FileJournal(root) as j:
        yield j


@pytest.fixture
def scope() -> Scope:
    return SCOPE
