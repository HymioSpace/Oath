"""测试共用夹具。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._support.storage import Storage


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "journal"


@pytest.fixture
def service(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "oath.sqlite3")
