"""`FileEventBuffer` —— JSONL 存储后端。只用标准库。

布局：

    <root>/<user_id>/<session_id>/<queue>.jsonl

按 scope 分目录而不是一个全局文件：隔离落在文件系统层面，三条队列也各占一个文件，
删一个 session 就是删一个目录。

**只支持单进程单写者**，这里不加文件锁 —— 与 journal 同样的取舍。

写失败时把文件截回原长度再抛错。崩溃留下的半行在**下一次写之前**截掉：`O_APPEND`
无条件写在文件尾，半行还在的话新输入会被接在它后面，两条记录粘成一条解析不了的
垃圾。只在读的时候丢半行挡不住这个 —— 那一行照样进了文件。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Sequence
from pathlib import Path

from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import QueueName
from core.protocol.datatype.scope import Scope, is_valid_scope_id
from core.protocol.error import CorruptRecordError, UnknownQueueError
from core.protocol.interface.buffer import EventBuffer
from core.utils.encoding import canonical_json

_LINE_TERMINATOR = "\n"
_FILE_MODE = 0o644
_READ_BLOCK = 1 << 16


def _last_newline(fd: int, size: int) -> int:
    """`[0, size)` 里最后一个换行符的下标，没有则 -1。

    从文件尾往前按块找：判断有没有半行不需要把整个文件读进内存。
    """
    pos = size
    while pos > 0:
        start = max(0, pos - _READ_BLOCK)
        block = os.pread(fd, pos - start, start)
        index = block.rfind(b"\n")
        if index != -1:
            return start + index
        pos = start
    return -1


def _truncate_torn_tail(fd: int) -> None:
    """把崩溃留下的半行截掉，让下一次 append 接在一条完整行后面。

    文件本来就以换行结尾时什么都不做 —— 常态不付这个 `pread` 之外的钱。
    """
    size = os.fstat(fd).st_size
    if size == 0 or os.pread(fd, 1, size - 1) == b"\n":
        return
    keep = _last_newline(fd, size) + 1
    os.ftruncate(fd, keep)
    os.fsync(fd)


class FileEventBuffer(EventBuffer):
    """per-scope 目录 + 每条队列一个 append-only 文件。"""

    def __init__(self, root: Path) -> None:
        self._root = root
        # 单进程里也可能有多个线程碰同一个文件（同一 scope 的消费线程，加上恢复时
        # 的扫描）。append 本身是原子的，但 ftruncate 不是。
        self._lock = threading.RLock()

    # -- 写入 --------------------------------------------------------------

    def put(self, scope: Scope, queue: str, items: Sequence[Inbound]) -> None:
        if not items:
            return
        path = self._path(scope, queue)
        blob = "".join(canonical_json(item.to_dict()) + _LINE_TERMINATOR for item in items)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, _FILE_MODE)
            try:
                _truncate_torn_tail(fd)
                good_size = os.fstat(fd).st_size
                data = blob.encode("utf-8")
                written = 0
                try:
                    while written < len(data):
                        written += os.write(fd, data[written:])
                    os.fsync(fd)
                except OSError:
                    os.ftruncate(fd, good_size)
                    os.fsync(fd)
                    raise
            finally:
                os.close(fd)

    def clear(self, scope: Scope, queue: str) -> None:
        path = self._path(scope, queue)
        with self._lock:
            if not path.exists():
                return
            fd = os.open(path, os.O_WRONLY)
            try:
                os.ftruncate(fd, 0)
                os.fsync(fd)
            finally:
                os.close(fd)

    # -- 读取 --------------------------------------------------------------

    def snapshot(self, scope: Scope, queue: str) -> tuple[Inbound, ...]:
        path = self._path(scope, queue)
        with self._lock:
            try:
                blob = path.read_bytes()
            except FileNotFoundError:
                return ()
        return self._parse(blob, path)

    def _parse(self, blob: bytes, path: Path) -> tuple[Inbound, ...]:
        """按字节切行，再逐行解码。

        先切后解码：崩溃可能把一个多字节字符劈成两半，`read_text` 会在读到半行的
        那一刻就抛 `UnicodeDecodeError`，连前面那些完整行都读不出来。
        """
        lines = blob.split(b"\n")
        if lines[-1] == b"":
            lines.pop()  # 结尾那个换行符自己切出来的空串
        else:
            lines.pop()  # 崩溃时正在写的那一行。只丢最后一行 —— 中间的损坏是别的问题
        items: list[Inbound] = []
        for number, raw in enumerate(lines, start=1):
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CorruptRecordError(f"{path}:{number} 不是合法 JSON: {exc}") from exc
            items.append(Inbound.from_dict(obj))
        return tuple(items)

    def scopes(self) -> tuple[Scope, ...]:
        """扫目录树找还有内容的 scope。

        空文件算没有 —— 那是被 `clear` 过还没等到新输入的 scope。
        """
        found: list[Scope] = []
        with self._lock:
            if not self._root.is_dir():
                return ()
            for user_dir in sorted(self._root.iterdir()):
                if not user_dir.is_dir():
                    continue
                for session_dir in sorted(user_dir.iterdir()):
                    if not session_dir.is_dir():
                        continue
                    if not any(
                        session_dir.joinpath(f"{q}.jsonl").stat().st_size
                        for q in QueueName.ALL
                        if session_dir.joinpath(f"{q}.jsonl").exists()
                    ):
                        continue
                    found.append(
                        Scope(
                            user_id=user_dir.name,
                            session_id=session_dir.name,
                        )
                    )
        return tuple(found)

    # -- 路径 --------------------------------------------------------------

    def _path(self, scope: Scope, queue: str) -> Path:
        if queue not in QueueName.ALL:
            raise UnknownQueueError(queue)
        # scope 的两个字段直接就是目录名。它们来自 credential，不能让它们里的
        # `../` 之类逃出 root —— 校验在拼路径之前，不是在写文件失败之后。
        if not is_valid_scope_id(scope.user_id) or not is_valid_scope_id(scope.session_id):
            raise UnknownQueueError(f"{scope.user_id}/{scope.session_id}")
        return self._root / scope.user_id / scope.session_id / f"{queue}.jsonl"


__all__ = ["FileEventBuffer"]
