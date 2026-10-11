"""共享 fixture：每个测试拿到一个全新的 journal，退出时关掉。"""
from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    # pyproject 的 pythonpath 也做了这件事，但显式加一次让直接 `pytest tests/`
    # 和 IDE 单独跑文件都成立。
    sys.path.insert(0, str(_ROOT))

from tests._support.journal_factory import (  # noqa: E402
    ADAPTERS,
    Harness,
    make_principal,
    make_scope,
)


@pytest.fixture(params=sorted(ADAPTERS))
def adapter_name(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def harness(adapter_name: str, tmp_path: Path) -> Iterator[Harness]:
    gen = ADAPTERS[adapter_name](tmp_path)
    instance = next(gen)
    try:
        yield instance
    finally:
        # 关闭由 factory 自己负责；这里只把生成器推到底拿到它的 finally。
        with pytest.raises(StopIteration):
            next(gen)


@pytest.fixture
def journal(harness: Harness) -> Any:
    return harness.journal


@pytest.fixture
def lease(harness: Harness) -> Any:
    return harness.lease


@pytest.fixture
def blobs(harness: Harness) -> Any:
    return harness.blobs


@pytest.fixture
def scope() -> Any:
    return make_scope()


@pytest.fixture
def principal() -> Any:
    return make_principal()
