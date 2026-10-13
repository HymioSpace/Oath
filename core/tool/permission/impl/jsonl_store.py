"""审批记录的 JSONL 存储。v1 的实现，只用标准库。

布局：

    <root>/<user_id>/<session_id>/permissions.jsonl

每行是一次写入：先是 `requested`，后是 `resolved`（可能没有）。当前状态是打开时
顺序 fold 出来的内存索引，所以「裁定只生效一次」这个语义由 fold 保证，不由写入
路径保证 —— 重放一份日志得到的最终状态和当初跑出来的一致。

照抄 `core/journal/impl/file_journal.py` 的几条硬做法：`O_APPEND` 打开、写前
校验、fsync 后才返回、写失败把文件截回原长度、只丢最后一行、损坏的中段抛错。

**只支持单进程单写者**，这里不加文件锁。审批从另一个连接进来的场景需要多进程，
那时这个存储不成立。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Final

from core.protocol.datatype.journal import RecordFormat
from core.protocol.datatype.permission import (
    PermissionRecord,
    PermissionRecordType,
    PermissionStatus,
)
from core.protocol.datatype.scope import Scope
from core.protocol.error import PermissionStoreError
from core.tool.permission.store import PermissionStore
from core.utils.encoding import JsonObject, canonical_json, parse_timestamp

#: 一个 scope 一个文件。文件名与 journal 不同，避免两种日志混在一起被当成对方。
PERMISSIONS_FILENAME: Final = "permissions.jsonl"

_RECORD_FIELDS: Final = frozenset(
    {
        "scope_user_id",
        "scope_session_id",
        "type",
        "tool_call_id",
        "invocation_id",
        "tool",
        "arguments",
        "status",
        "requested_dt",
        "expires_dt",
        "decided_by",
        "decided_dt",
        "reason",
    }
)


def _require_str(value: JsonObject, field: str) -> str:
    got = value.get(field)
    if not isinstance(got, str) or not got:
        raise PermissionStoreError(f"字段 {field!r} 必须是非空字符串，得到 {got!r}")
    return got


def _optional_str(value: JsonObject, field: str) -> str | None:
    got = value.get(field)
    if got is None:
        return None
    if not isinstance(got, str):
        raise PermissionStoreError(f"字段 {field!r} 必须是字符串或 null，得到 {type(got).__name__}")
    return got


def _require_object(value: JsonObject, field: str) -> JsonObject:
    got = value.get(field)
    if not isinstance(got, dict):
        raise PermissionStoreError(f"字段 {field!r} 必须是 JSON 对象，得到 {type(got).__name__}")
    return got


def parse_record(line: str, scope: Scope) -> PermissionRecord:
    """从一行落盘文本还原出记录。**结构**校验，不做业务语义校验。

    损坏就抛错，不静默跳过 —— 静默跳过等于丢数据，而丢的是「谁批过什么」。
    """
    try:
        raw = json.loads(line)
    except ValueError as exc:
        raise PermissionStoreError(f"权限记录不是合法 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise PermissionStoreError(f"权限记录必须是 JSON 对象，得到 {type(raw).__name__}")
    unknown = set(raw) - _RECORD_FIELDS
    if unknown:
        raise PermissionStoreError(f"权限记录有未知字段: {sorted(unknown)}")

    raw_scope = Scope(
        user_id=_require_str(raw, "scope_user_id"),
        session_id=_require_str(raw, "scope_session_id"),
    )
    if raw_scope != scope:
        # 目录已经按 scope 分了，文件里的 scope 对不上就是有人挪过文件或抄错了行。
        raise PermissionStoreError(f"记录 scope {raw_scope} 与所在目录的 {scope} 不一致")

    type_ = _require_str(raw, "type")
    if type_ not in PermissionRecordType.ALL:
        raise PermissionStoreError(f"未知的记录类型 {type_!r}")
    status = _require_str(raw, "status")
    if status not in PermissionStatus.ALL:
        raise PermissionStoreError(f"未知的权限状态 {status!r}")

    # 时间戳要能解析：过期判定与展示都靠它，解析不了的行等于没有有效期。
    requested_dt = _require_str(raw, "requested_dt")
    expires_dt = _require_str(raw, "expires_dt")
    for field, value in (("requested_dt", requested_dt), ("expires_dt", expires_dt)):
        try:
            parse_timestamp(value)
        except ValueError as exc:
            raise PermissionStoreError(
                f"字段 {field!r} 不是合法的 RFC 3339 时刻: {value!r}"
            ) from exc

    return PermissionRecord(
        scope=raw_scope,
        tool_call_id=_require_str(raw, "tool_call_id"),
        invocation_id=_require_str(raw, "invocation_id"),
        tool=_require_str(raw, "tool"),
        arguments=_require_object(raw, "arguments"),
        status=status,
        requested_dt=requested_dt,
        expires_dt=expires_dt,
        decided_by=_optional_str(raw, "decided_by"),
        decided_dt=_optional_str(raw, "decided_dt"),
        reason=_optional_str(raw, "reason"),
    )


class _ScopeHandle:
    """一个 scope 的打开状态：fd、当前状态索引、good_size。

    「当前状态」在内存里：open 时顺序 fold 整个文件重建，之后每次写入顺手更新。
    与事件条数线性相关，所以这是个小存储才付得起的代价 —— 它的规模是「等人批的
    次数」，不是对话轮数。
    """

    def __init__(self, scope: Scope, path: Path, *, fsync: bool, truncate_torn_tail: bool) -> None:
        self.scope = scope
        self.path = path
        self._fsync = fsync
        self._truncate_torn_tail = truncate_torn_tail
        self._fd: int | None = None
        #: `invocation_id` → 当前状态。最后一行赢。
        self._current: dict[str, PermissionRecord] = {}
        #: 写入顺序，用来让 `pending()` 的顺序稳定。
        self._order: list[str] = []
        self._size = 0

    # -- 生命周期 ---------------------------------------------------------

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._replay()
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        self._fd = os.open(self.path, flags, RecordFormat.FILE_MODE)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def _replay(self) -> None:
        """顺序 fold 整个文件，重建当前状态。

        只有**最后一行**可能被截断（崩溃时正在写的那一行），丢弃它。中间的损坏
        一律抛错 —— 静默跳过等于把一次审批从历史里抹掉。
        """
        if not self.path.exists():
            return
        good_bytes = 0
        torn = False
        with self.path.open("rb") as fh:
            for raw in fh:
                if not raw.endswith(b"\n"):
                    torn = True
                    break
                good_bytes += len(raw)
                self._absorb(parse_record(raw.decode("utf-8"), self.scope))
        self._size = good_bytes
        if torn and self._truncate_torn_tail:
            self._truncate(good_bytes)

    def _absorb(self, record: PermissionRecord) -> None:
        """把一条记录并进内存索引。fold 与写入走同一条路径，语义不会分家。"""
        known = self._current.get(record.invocation_id)
        if known is None:
            self._order.append(record.invocation_id)
        elif not known.is_resolved and record.is_resolved:
            self._order.remove(record.invocation_id)
        self._current[record.invocation_id] = record

    def _truncate(self, size: int) -> None:
        fd = os.open(self.path, os.O_WRONLY)
        try:
            os.ftruncate(fd, size)
            if self._fsync:
                os.fsync(fd)
        finally:
            os.close(fd)

    # -- 写入 -------------------------------------------------------------

    def append(self, record: PermissionRecord) -> None:
        """追加一行并保证落盘。

        返回前必须 fsync —— 这是唯一的耐久性承诺。写失败时把文件截回原长度再抛：
        半行留在文件里会被下一次写入接在后面，把两条记录粘成一条垃圾。
        """
        if self._fd is None:
            raise PermissionStoreError(f"scope {self.scope} 未打开")
        data = (canonical_json(record.to_dict()) + RecordFormat.LINE_TERMINATOR).encode("utf-8")
        good_size = self._size
        written = 0
        try:
            while written < len(data):
                written += os.write(self._fd, data[written:])
            if self._fsync:
                os.fsync(self._fd)
        except OSError as exc:
            self._truncate(good_size)
            raise PermissionStoreError(f"写权限记录失败: {exc}") from exc
        self._size = good_size + len(data)
        self._absorb(record)

    # -- 读取 -------------------------------------------------------------

    def known(self, invocation_id: str) -> PermissionRecord | None:
        return self._current.get(invocation_id)

    def pending(self) -> tuple[PermissionRecord, ...]:
        return tuple(
            self._current[key] for key in self._order if not self._current[key].is_resolved
        )


class FilePermissionStore(PermissionStore):
    """JSONL 实现的审批存储。"""

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

    def close(self) -> None:
        with self._lock:
            for handle in self._handles.values():
                handle.close()
            self._handles.clear()

    def __enter__(self) -> FilePermissionStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _handle(self, scope: Scope) -> _ScopeHandle:
        handle = self._handles.get(scope)
        if handle is None:
            # Scope 构造时已校验字符集，所以这里拼路径是安全的。
            handle = _ScopeHandle(
                scope,
                self._root / scope.user_id / scope.session_id / PERMISSIONS_FILENAME,
                fsync=self._fsync,
                truncate_torn_tail=self._truncate_torn_tail,
            )
            handle.open()
            self._handles[scope] = handle
        return handle

    # -- PermissionStore 接口 ---------------------------------------------

    def record(self, record: PermissionRecord) -> None:
        with self._lock:
            handle = self._handle(record.scope)
            known = handle.known(record.invocation_id)
            if known is not None:
                # 工具系统按 `invocation_id` 去重，走到这里说明去重漏了。
                raise PermissionStoreError(
                    f"invocation_id {record.invocation_id!r} 在 scope {record.scope} "
                    f"已有记录（状态 {known.status}）"
                )
            handle.append(record)

    def resolve(self, record: PermissionRecord) -> bool:
        if not record.is_resolved:
            raise PermissionStoreError(f"resolve 只接受已终结的状态，得到 {record.status!r}")
        with self._lock:
            handle = self._handle(record.scope)
            known = handle.known(record.invocation_id)
            if known is None or known.is_resolved:
                # 没人等它，或者已经被别的裁定关掉了。两种都不写盘。
                return False
            if known.tool_call_id != record.tool_call_id or known.tool != record.tool:
                raise PermissionStoreError(
                    f"裁定与请求对不上（请求 {known.tool}/{known.tool_call_id}，"
                    f"裁定 {record.tool}/{record.tool_call_id}）"
                )
            handle.append(record)
            return True

    def get(self, scope: Scope, invocation_id: str) -> PermissionRecord | None:
        with self._lock:
            return self._handle(scope).known(invocation_id)

    def pending(self, scope: Scope) -> tuple[PermissionRecord, ...]:
        with self._lock:
            return self._handle(scope).pending()


__all__ = ["PERMISSIONS_FILENAME", "FilePermissionStore", "parse_record"]
