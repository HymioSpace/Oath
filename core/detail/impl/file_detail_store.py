"""文件后端的 detail store。布局与 journal 一致，按 scope 分目录。

    <root>/<user_id>/<session_id>/<key>.txt

只用标准库。key 由 core 生成并限定字符集，不接受模型给的路径片段。
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from core.protocol.datatype import Scope
from core.protocol.interface.detail import DetailStore

#: key 的合法字符集。不含分隔符和点开头的组合 —— key 会被拼进路径。
KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def is_valid_detail_key(key: str) -> bool:
    return isinstance(key, str) and KEY_PATTERN.match(key) is not None


class FileDetailStore(DetailStore):
    """一个 key 一个文件，写临时文件后 rename 落位。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    def put(self, scope: Scope, key: str, content: str) -> None:
        if not is_valid_detail_key(key):
            raise ValueError(f"detail_key 不合法: {key!r}")
        path = self._root / scope.user_id / scope.session_id
        path.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=path, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path / f"{key}.txt")
        except BaseException:
            os.unlink(temporary)
            raise

    def get(self, scope: Scope, key: str) -> str | None:
        if not is_valid_detail_key(key):
            return None
        try:
            return (self._root / scope.user_id / scope.session_id / f"{key}.txt").read_text(
                encoding="utf-8"
            )
        except (OSError, ValueError):
            return None


__all__ = ["FileDetailStore", "is_valid_detail_key"]
