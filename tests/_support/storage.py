"""测试用的带类型 Storage 外壳。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core.storage.database import Database
from core.storage.service import StorageService


class Storage:
    """`StorageService` 的带类型外壳。

    core 那边的 `call` 与 `transaction` 还没有注解（`[tool.mypy] strict = true` 下会
    报 `no-untyped-call`）。这里补一层注解，测试本身保持干净 —— 给 core 补注解是
    另一件事，不该由测试来承担。
    """

    def __init__(self, path: Path) -> None:
        self._inner = StorageService(Database(path))

    def call(self, operation: str, **params: Any) -> Any:
        return self._inner.call(operation, **params)  # type: ignore[no-untyped-call]

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._inner.database.transaction() as db:
            yield db
