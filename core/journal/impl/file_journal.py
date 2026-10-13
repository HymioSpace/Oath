"""JSONL 存储后端。v1 的默认实现，只用标准库。

布局：

    <root>/<user_id>/<session_id>/events.jsonl
    <root>/<user_id>/<session_id>/state.jsonl

按 scope 分目录而不是一个全局文件：隔离落在文件系统层面（删一个 session 就是删
一个目录），不同 session 并发写互不干扰，单个 session 可独立归档。

**只支持单进程单写者**，这里不加文件锁。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from itertools import islice
from pathlib import Path
from typing import BinaryIO

from core.journal import messages as projection
from core.journal.cursor import (
    beginning,
    check_kind,
    parse_cursor,
    record_fingerprint,
    require_object,
)
from core.journal.validation import (
    parse_event_record,
    parse_state_record,
    validate_event_type,
    validate_idempotency_key,
    validate_occur_dt,
    validate_payload,
    validate_state_payload,
)
from core.protocol.datatype import (
    AgentState,
    AppendResult,
    Cursor,
    CursorKind,
    Record,
    RecordFormat,
    Recovery,
    Scope,
    StateRecord,
    StateRef,
)
from core.protocol.error import CursorError, IdempotencyKeyConflict
from core.protocol.interface import Journal
from core.utils.encoding import JsonObject, canonical_json, utc_now_iso

_READ_BLOCK = 1 << 16
_SUPPORTED_CURSOR_KINDS = frozenset({CursorKind.LINE})


def _find_last_newline(fh: BinaryIO, size: int) -> int:
    """从 `fh` 的 `[0, size)` 里找最后一个换行符的下标，没有则返回 -1。

    从文件尾往前按块找，这样读状态日志的尾部不需要把整个文件读进内存。
    """
    pos = size
    while pos > 0:
        start = max(0, pos - _READ_BLOCK)
        fh.seek(start)
        block = fh.read(pos - start)
        index = block.rfind(b"\n")
        if index != -1:
            return start + index
        pos = start
    return -1


class _ScopeHandle:
    """一个 scope 的打开状态：文件描述符、幂等索引、水位线。

    幂等索引常驻内存，与事件条数线性相关；必须在 open 时从日志重建，否则重启后
    幂等失效。

    对外暴露的游标（`AppendResult.cursor`、`Recovery.state.cursor_end`）一律定位**事件**
    日志。状态快照本身的位置不外露，调用方要的是它覆盖到哪儿，见
    `StateRef.covered_through`。
    """

    def __init__(
        self,
        scope: Scope,
        directory: Path,
        *,
        fsync: bool,
        truncate_torn_tail: bool,
    ) -> None:
        self.scope = scope
        self.directory = directory
        self.events_path = directory / RecordFormat.EVENTS_FILENAME
        self.state_path = directory / RecordFormat.STATE_FILENAME
        self._fsync = fsync
        self._truncate_torn_tail = truncate_torn_tail
        self._events_fd: int | None = None
        self._state_fd: int | None = None
        self._index: dict[str, tuple[str, Cursor]] = {}
        self._event_count = 0
        self._events_size = 0
        self.last_state: StateRecord | None = None

    # -- 打开 / 关闭 ------------------------------------------------------

    def open(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._scan_events()
        self._read_state_tail()
        self._events_fd = self._open_append(self.events_path)
        self._state_fd = self._open_append(self.state_path)

    def close(self) -> None:
        for attr in ("_events_fd", "_state_fd"):
            fd = getattr(self, attr)
            if fd is not None:
                os.close(fd)
                setattr(self, attr, None)

    @staticmethod
    def _open_append(path: Path) -> int:
        # O_APPEND 保证每次 write 都落在文件尾，不依赖自己记的偏移量。
        return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, RecordFormat.FILE_MODE)

    def _scan_events(self) -> None:
        """扫描事件日志，建立幂等索引并记下水位线。

        只有**最后一行**可能被截断（崩溃时正在写的那一行），丢弃它。
        中间的损坏一律抛错 —— 静默跳过等于丢数据。
        """
        if not self.events_path.exists():
            return

        good_bytes = 0
        line_no = 0
        torn = False
        with self.events_path.open("rb") as fh:
            for raw in fh:
                if not raw.endswith(b"\n"):
                    torn = True  # 崩溃留下的半行
                    break
                line_no += 1
                good_bytes += len(raw)
                record = parse_event_record(raw.decode("utf-8"), self.scope)
                key = record["idempotency_key"]
                fingerprint = record_fingerprint(
                    record["type"], record["payload_schema_version"], record["payload"]
                )
                # 同一键出现多行只在日志被手工改过时可能；后写的赢，与 append
                # 的幂等语义一致。
                self._index[key] = (fingerprint, Cursor(CursorKind.LINE, line_no))

        self._event_count = line_no
        self._events_size = good_bytes
        if torn and self._truncate_torn_tail:
            self._truncate(self.events_path, good_bytes)

    def _read_state_tail(self) -> None:
        """只读状态日志的**尾部**（最后一条完整记录），不做全量扫描。"""
        if not self.state_path.exists():
            return

        with self.state_path.open("r+b") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size == 0:
                return
            last_nl = _find_last_newline(fh, size)
            if last_nl == -1:
                # 整个文件都没有完整行 —— 第一次写就崩了。
                if self._truncate_torn_tail:
                    self._truncate(self.state_path, 0)
                return
            if last_nl != size - 1 and self._truncate_torn_tail:
                self._truncate(self.state_path, last_nl + 1)
            prev_nl = _find_last_newline(fh, last_nl)
            start = prev_nl + 1
            fh.seek(start)
            text = fh.read(last_nl - start).decode("utf-8")

        self.last_state = parse_state_record(text, self.scope)

    def _truncate(self, path: Path, size: int) -> None:
        fd = os.open(path, os.O_WRONLY)
        try:
            os.ftruncate(fd, size)
            if self._fsync:
                os.fsync(fd)
        finally:
            os.close(fd)

    # -- 写入 -------------------------------------------------------------

    def _append_line(self, path: Path, fd: int | None, obj: object, good_size: int) -> None:
        """追加一行并保证落盘。

        返回前必须 fsync —— 这是唯一的耐久性承诺。写失败时把文件截回原长度
        再抛错：半行留在文件里会被下一次 append 接在后面，把两条记录粘成
        一条无法解析的垃圾。
        """
        if fd is None:
            raise RuntimeError(f"scope {self.scope} 未打开")
        data = (canonical_json(obj) + RecordFormat.LINE_TERMINATOR).encode("utf-8")
        written = 0
        try:
            while written < len(data):
                written += os.write(fd, data[written:])
            if self._fsync:
                os.fsync(fd)
        except OSError:
            self._truncate(path, good_size)
            raise

    def append_event(self, obj: object) -> Cursor:
        cursor = Cursor(CursorKind.LINE, self._event_count + 1)
        size = self._events_size
        self._append_line(self.events_path, self._events_fd, obj, size)
        self._event_count += 1
        self._events_size = size + len(canonical_json(obj)) + len(RecordFormat.LINE_TERMINATOR)
        return cursor

    def append_state(self, record: StateRecord) -> None:
        # good_size 传 0：状态日志没有追加途中的崩溃恢复需求——真崩了就是
        # 半行，下次 open 时按截断尾行处理。
        self._append_line(self.state_path, self._state_fd, record, 0)
        self.last_state = record

    def note_idempotent(self, key: str, fingerprint: str, cursor: Cursor) -> None:
        self._index[key] = (fingerprint, cursor)

    def lookup_idempotent(self, key: str) -> tuple[str, Cursor] | None:
        return self._index.get(key)

    # -- 读取 -------------------------------------------------------------

    def iter_records(self, after: Cursor) -> Iterator[Record]:
        """顺序读出 `after` 之后的全部记录。"""
        if not self.events_path.exists():
            return
        with self.events_path.open("rb") as fh:
            for line_no, raw in enumerate(fh, start=1):
                if line_no <= after.value:
                    continue
                if not raw.endswith(b"\n"):
                    break  # 截断尾行
                cursor = Cursor(CursorKind.LINE, line_no)
                yield Record.of(cursor, parse_event_record(raw.decode("utf-8"), self.scope))


class FileJournal(Journal):
    """JSONL 实现的 journal。"""

    def __init__(
        self,
        root: str | Path,
        *,
        fsync: bool = True,
        truncate_torn_tail: bool = True,
    ) -> None:
        self._root = Path(root)
        self._fsync = fsync
        self._truncate_torn_tail = truncate_torn_tail
        self._handles: dict[Scope, _ScopeHandle] = {}
        self._lock = threading.Lock()

    # -- 生命周期 ---------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            for handle in self._handles.values():
                handle.close()
            self._handles.clear()

    def __enter__(self) -> FileJournal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _handle(self, scope: Scope) -> _ScopeHandle:
        handle = self._handles.get(scope)
        if handle is None:
            # Scope 构造时已校验字符集，所以这里拼路径是安全的。
            handle = _ScopeHandle(
                scope,
                self._root / scope.user_id / scope.session_id,
                fsync=self._fsync,
                truncate_torn_tail=self._truncate_torn_tail,
            )
            handle.open()
            self._handles[scope] = handle
        return handle

    # -- Journal 接口 -----------------------------------------------------

    @property
    def cursor_kind(self) -> str:
        return CursorKind.LINE

    def append(
        self,
        scope: Scope,
        type: str,
        payload: JsonObject,
        *,
        payload_schema_version: int,
        idempotency_key: str,
        occur_dt: str | None = None,
    ) -> AppendResult:
        validate_event_type(type)
        validate_payload(type, payload_schema_version, require_object(payload, "payload"))
        validate_idempotency_key(idempotency_key)
        validate_occur_dt(occur_dt)

        fingerprint = record_fingerprint(type, payload_schema_version, payload)
        with self._lock:
            handle = self._handle(scope)
            hit = handle.lookup_idempotent(idempotency_key)
            if hit is not None:
                known_fingerprint, cursor = hit
                if known_fingerprint == fingerprint:
                    return AppendResult(cursor=cursor, record_id=None, idempotent_hit=True)
                raise IdempotencyKeyConflict(
                    f"幂等键 {idempotency_key!r} 已存在但内容不同（scope={scope}）"
                )

            record: JsonObject = {
                "user_id": scope.user_id,
                "session_id": scope.session_id,
                "type": type,
                "payload": payload,
                "payload_schema_version": payload_schema_version,
                "idempotency_key": idempotency_key,
                "occur_dt": occur_dt,
                "commit_dt": utc_now_iso(),
            }
            cursor = handle.append_event(record)
            handle.note_idempotent(idempotency_key, fingerprint, cursor)
            return AppendResult(cursor=cursor, record_id=None, idempotent_hit=False)

    def commit_state(self, state: AgentState) -> StateRef:
        scope = state.scope
        payload = state.to_json()
        version = state.payload_schema_version
        validate_state_payload(version, require_object(payload, "payload"))
        check_kind(state.cursor_end, _SUPPORTED_CURSOR_KINDS)
        cursor_start = state.cursor_start
        if cursor_start is not None:
            check_kind(cursor_start, _SUPPORTED_CURSOR_KINDS)
            if cursor_start.value > state.cursor_end.value:
                raise CursorError(
                    f"cursor_start({cursor_start.value}) 不能大于 "
                    f"cursor_end({state.cursor_end.value})"
                )

        commit_dt = utc_now_iso()
        record = StateRecord(
            user_id=scope.user_id,
            session_id=scope.session_id,
            commit_dt=commit_dt,
            payload_schema_version=version,
            payload=payload,
            cursor_start=cursor_start.to_json() if cursor_start else None,
            cursor_end=state.cursor_end.to_json(),
        )
        with self._lock:
            handle = self._handle(scope)
            handle.append_state(record)
        return StateRef(covered_through=state.cursor_end, commit_dt=commit_dt)

    def recover(self, scope: Scope) -> Recovery:
        with self._lock:
            handle = self._handle(scope)
            snapshot = handle.last_state
            if snapshot is None:
                state = AgentState.covering(scope, beginning(CursorKind.LINE))
            else:
                cursor_start = snapshot["cursor_start"]
                state = AgentState.from_record(
                    scope,
                    snapshot["payload"],
                    parse_cursor(cursor_start, supported=_SUPPORTED_CURSOR_KINDS)
                    if cursor_start is not None
                    else None,
                    parse_cursor(snapshot["cursor_end"], supported=_SUPPORTED_CURSOR_KINDS),
                    snapshot["payload_schema_version"],
                )

            records = list(handle.iter_records(beginning(CursorKind.LINE)))
            messages = projection.project(records, state.cursor_end)
            pending = [r for r in records if not state.covers(r.cursor)]

        return Recovery(state=state, messages=messages, pending=pending)

    def read_range(
        self,
        scope: Scope,
        cursor: Cursor,
        *,
        limit: int | None = None,
    ) -> Iterator[Record]:
        check_kind(cursor, _SUPPORTED_CURSOR_KINDS)
        if limit is not None and limit < 0:
            raise CursorError(f"limit 不能为负，得到 {limit}")
        with self._lock:
            handle = self._handle(scope)
        stream = handle.iter_records(cursor)
        yield from stream if limit is None else islice(stream, limit)


__all__ = ["FileJournal"]
