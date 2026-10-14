"""`FileEventBuffer` 的写失败与半行。

与 `test_journal_durability.py` 同一套理由：**人工切出半行**。`os.write()` 返回之后
内容已经在页缓存里，杀进程留不下半行；真正的半行要靠掉电。

接口层面的行为在 `tests/conformance/test_buffer_conformance.py`，这里只测这个文件
实现自己担的那份责任：写失败回滚、崩溃留下的半行不污染下一次 append。
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from core.buffer import FileEventBuffer
from core.protocol.datatype import Inbound, InboundKind, Scope


def _path(root: Path, scope: Scope, queue: str) -> Path:
    return root / scope.user_id / scope.session_id / f"{queue}.jsonl"


def item(index: int) -> Inbound:
    return Inbound(
        kind=InboundKind.USER,
        source=f"src-{index}",
        payload={"content": f"第 {index} 条"},
        source_id=f"id-{index}",
        occur_dt="2026-01-01T00:00:00.000000Z",
    )


@pytest.fixture
def scope() -> Scope:
    return Scope(user_id="u1", session_id="s1")


def _ids(items: tuple[Inbound, ...]) -> list[str]:
    return [i.source_id for i in items]


# --------------------------------------------------------------------------
# 半行
# --------------------------------------------------------------------------


def test_a_torn_tail_is_dropped_and_the_rest_survives(tmp_path: Path, scope: Scope) -> None:
    buffer = FileEventBuffer(tmp_path)
    buffer.put(scope, "user", (item(1), item(2)))
    path = _path(tmp_path, scope, "user")

    with path.open("ab") as fh:
        fh.write(b'{"kind":"user","sou')

    assert _ids(buffer.snapshot(scope, "user")) == ["id-1", "id-2"]


def test_a_torn_tail_is_truncated_before_the_next_append(tmp_path: Path, scope: Scope) -> None:
    """`O_APPEND` 无条件写在文件尾。半行还在的话新输入会被接在它后面，两条记录
    粘成一条解析不了的垃圾 —— 只在读的时候丢半行挡不住这个。"""
    buffer = FileEventBuffer(tmp_path)
    buffer.put(scope, "user", (item(1),))
    path = _path(tmp_path, scope, "user")
    good = path.read_bytes()
    with path.open("ab") as fh:
        fh.write(b'{"broken')

    buffer.put(scope, "user", (item(2),))

    assert path.read_bytes().startswith(good)
    assert _ids(buffer.snapshot(scope, "user")) == ["id-1", "id-2"]


def test_a_character_split_across_the_tail_is_not_a_decode_error(
    tmp_path: Path, scope: Scope
) -> None:
    """崩溃可能把一个多字节字符劈成两半。`read_text` 读到半行那一刻就抛
    `UnicodeDecodeError`，连前面那些完整行都读不出来。"""
    buffer = FileEventBuffer(tmp_path)
    buffer.put(scope, "user", (item(1), item(2)))
    path = _path(tmp_path, scope, "user")

    with path.open("ab") as fh:
        fh.write("第".encode()[:2])  # 一个汉字的头两个字节

    assert _ids(buffer.snapshot(scope, "user")) == ["id-1", "id-2"]


# --------------------------------------------------------------------------
# 写失败回滚
# --------------------------------------------------------------------------


def test_a_failed_put_rolls_back_to_the_previous_content(tmp_path: Path, scope: Scope) -> None:
    buffer = FileEventBuffer(tmp_path)
    buffer.put(scope, "user", (item(1),))
    good = _path(tmp_path, scope, "user").read_bytes()

    real_write = os.write

    def flaky(fd: int, data: bytes) -> int:
        real_write(fd, data[: len(data) // 2])
        raise OSError(errno.EIO, "写设备故障")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "write", flaky)
        with pytest.raises(OSError):
            buffer.put(scope, "user", (item(2),))

    assert _path(tmp_path, scope, "user").read_bytes() == good
    assert _ids(buffer.snapshot(scope, "user")) == ["id-1"]
