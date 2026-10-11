"""JSONL journal —— Oath 的默认存储。

文件布局：`<root>/<user>/<session>/journal.jsonl`，每个 scope 一个文件，一行一次提交。

**一行一次 commit，不是一行一条记录。** feature07 要求一批记录全有或全无。把整批
放进一行，原子性就等于「这一行要么完整要么不存在」，不用额外维护 begin/end 标记。

**单写者。** 进程持有 `flock(LOCK_EX)` 直到 close，由操作系统保证同一时刻只有一个
写进程。第二个写进程会在 append 上失败，所以不声明 `multi_process` 能力。

**已知限制**（多进程 owner failover 阶段处理）：
- `commits` / `dedup` 两个索引是内存态，随日志长度增长；重开时全量重扫重建。
- 每次 append 都 `fsync`，正确优先于吞吐。
"""
from __future__ import annotations

import asyncio
import base64
import fcntl
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from core.codec import canonical_json, checksum_of, from_iso, sha256_hex, to_iso
from core.errors import (
    FencedOut,
    InjectedCrash,
    LogCorrupt,
    ScopeViolation,
    StateVersionConflict,
    ValidationError,
)
from core.journal.api import (
    BLOB_STORE,
    REOPEN,
    SIMULATE_CRASH,
    BlobStore,
    Journal,
    Lease,
    StateWriter,
    Writer,
)
from core.journal.cursor import Cursor, CursorCodec, check_page_limit
from core.journal.records import (
    CommitBatch,
    CommitReceipt,
    Head,
    JournalLimits,
    JournalRecord,
    Principal,
    ReadPage,
    ReadRequest,
    RecordDraft,
    Scope,
)
from core.journal.validation import payload_hash_of, validate_batch

#: 日志行格式版本。读到不认识的版本必须停下，不猜。
LINE_VERSION = 1


def _scope_dir_name(scope: Scope) -> str:
    """scope → 目录名。

    用 urlsafe base64 而不是 sha256：出问题时能从目录名反解回 `(user_id, session_id)`，
    sha256 更短但不可读。base64 的字符集不含 `/`，不会造成路径穿越。
    """
    user = base64.urlsafe_b64encode(scope.user_id.encode("utf-8")).decode("ascii")
    session = base64.urlsafe_b64encode(scope.session_id.encode("utf-8")).decode("ascii")
    return f"{user}/{session}"


@dataclass
class _StoredCommit:
    """已提交批次的元信息。同 commit_id 重试时用它重建 receipt。

    派生数据，重开时从日志重扫重建。
    """

    checksum: str
    first_seq: int
    record_count: int
    committed_at: datetime
    state_version: int | None = None
    owner_epoch: int | None = None


@dataclass
class _Stream:
    """一个 `(user_id, session_id)` 的日志流。

    日志内容不留在内存里：读的时候按 `index` 里的字节偏移直接 seek。
    这里存的 commits / dedup / index 都是派生数据。
    """

    scope: Scope
    commits: dict[str, _StoredCommit] = field(default_factory=dict)
    #: (first_seq, record_count, byte_offset)，每次 commit 一项
    index: list[tuple[int, int, int]] = field(default_factory=list)
    #: event_id -> (payload_hash, commit_id)
    dedup: dict[str, tuple[str, str]] = field(default_factory=dict)
    head_seq: int = 0
    state_version: int = 0
    owner_epoch: int = 0
    holder: str | None = None


class _ScopeFile:
    """一个 scope 的日志文件：打开、加锁、重放、追加。"""

    def __init__(self, root: str, scope: Scope) -> None:
        self.scope = scope
        directory = os.path.join(root, _scope_dir_name(scope))
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, "journal.jsonl")
        # 目录项本身也要落盘：只 fsync 文件不保证文件名进了目录。首次创建时做一次。
        existed = os.path.exists(self.path)
        self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        if not existed:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        # 单写者由操作系统保证。拿不到锁就是别的进程正持有这个 scope。
        fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        #: 已确认完整的字节数。文件比它长 = 最后一行没写完。
        self.good_bytes = os.fstat(self._fd).st_size

    def read_all_lines(self) -> list[tuple[int, dict[str, Any]]]:
        """重放用。返回 [(行起始偏移, 解析后的对象)]。

        只有末尾那行允许不完整 —— 进程写到一半就没了。按行迭代天然分得清：一个
        不以换行结尾的分块只可能是最后一个分块。

        完整的一行解析不出来就是真损坏，抛 LogCorrupt。不能在这里停下来了事：
        调用方据此把文件截到「已知完整前缀」，等于把后面还完好的提交一起丢掉，
        而且丢得没有声音。
        """
        entries: list[tuple[int, dict[str, Any]]] = []
        offset = 0
        good = 0
        with open(self.path, "rb") as fh:
            for raw in fh:
                if not raw.endswith(b"\n"):
                    # 末尾的半行：写到一半进程就没了。丢掉它，不算一次提交。
                    break
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise LogCorrupt(
                        f"{self.path} 偏移 {offset} 处的完整行解析失败: {exc}"
                    ) from exc
                entries.append((offset, obj))
                offset += len(raw)
                good = offset
        self.good_bytes = good
        return entries

    def truncate_to_good(self) -> None:
        """把没写完的最后一行截掉。这是崩溃后唯一的修复动作。"""
        os.ftruncate(self._fd, self.good_bytes)
        os.fsync(self._fd)

    def append_line(self, obj: dict[str, Any]) -> None:
        """追加一行并 fsync。行要么完整落地，要么在下次重放时被截掉。"""
        line = (canonical_json(obj) + "\n").encode("utf-8")
        view = memoryview(line)
        while view:
            # 短写循环。进程死在循环中间 = 最后一行不完整，由 read_all_lines 识别。
            written = os.write(self._fd, view)
            view = view[written:]
        os.fsync(self._fd)
        self.good_bytes += len(line)

    @property
    def size(self) -> int:
        return os.fstat(self._fd).st_size

    def write_raw(self, data: bytes) -> None:
        """不经封装的裸写入。只给崩溃注入用 —— 故意写半行。"""
        os.write(self._fd, data)
        os.fsync(self._fd)

    def read_line_at(self, offset: int) -> dict[str, Any]:
        with open(self.path, "rb") as fh:
            fh.seek(offset)
            line: dict[str, Any] = json.loads(fh.readline())
            return line

    def close(self) -> None:
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)


class FileBlobStore(BlobStore):
    """内容寻址的 blob 存储：一个 hash 一个文件。"""

    def __init__(self, root: str) -> None:
        self._root = os.path.join(root, "blobs")
        os.makedirs(self._root, exist_ok=True)

    def _path(self, blob_id: str) -> str:
        return os.path.join(self._root, blob_id[:2], blob_id)

    async def put(self, data: bytes) -> tuple[str, int]:
        digest = sha256_hex(data)
        path = self._path(digest)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # O_EXCL：内容寻址，同内容必然同路径，已存在就是已写好。
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return digest, len(data)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        return digest, len(data)

    async def get(self, blob_id: str) -> bytes:
        try:
            with open(self._path(blob_id), "rb") as fh:
                return fh.read()
        except FileNotFoundError as exc:
            raise KeyError(f"blob 不存在: {blob_id}") from exc

    async def verify(self, blob_id: str, expected_hash: str) -> bool:
        path = self._path(blob_id)
        if expected_hash != blob_id:
            return False
        try:
            with open(path, "rb") as fh:
                return sha256_hex(fh.read()) == blob_id
        except FileNotFoundError:
            return False


class JsonlJournal(Journal):
    """JSONL journal。Oath 的默认存储：只用标准库，装上就持久化。"""

    def __init__(
        self,
        root: str | os.PathLike[str],
        limits: JournalLimits | None = None,
    ) -> None:
        self._root = os.fspath(root)
        os.makedirs(self._root, exist_ok=True)
        self._limits = limits if limits is not None else JournalLimits()
        self._cursors = CursorCodec()
        self._blobs = FileBlobStore(self._root)
        self._files: dict[tuple[str, str], _ScopeFile] = {}
        self._streams: dict[tuple[str, str], _Stream] = {}
        #: 单进程内的批间串行。跨进程由 flock 保证。
        self._lock = asyncio.Lock()
        self._crash_next_commit = False

    # ---------- 能力与部件 ----------

    @property
    def capabilities(self) -> frozenset[str]:
        # 没有 MULTI_PROCESS：flock 让第二个写进程直接失败，那不是「支持」。
        return frozenset({
            REOPEN,
            SIMULATE_CRASH,
            BLOB_STORE,
        })

    @property
    def limits(self) -> JournalLimits:
        return self._limits

    @property
    def blobs(self) -> FileBlobStore:
        return self._blobs

    def lease(self) -> JsonlLease:
        return JsonlLease(self)

    def _path_for(self, scope: Scope) -> str:
        return os.path.join(self._root, _scope_dir_name(scope), "journal.jsonl")

    def reopen(self) -> JsonlJournal:
        """在同一个目录上开一个新实例。close() 之后调用。"""
        return JsonlJournal(self._root, limits=self._limits)

    def crash_on_next_commit(self) -> None:
        """让下一次 commit 写到一半崩溃（feature07 的崩溃注入钩子）。

        写的是**真的**半行，不是内存里的假象：半个 JSON、没有换行、然后抛
        `InjectedCrash`。之后 journal 必须自己把这条截掉，且全程不暴露半个 commit。
        """
        self._crash_next_commit = True

    # ---------- 文件与重放 ----------

    def _stream(self, scope: Scope) -> _Stream:
        """取流，不存在就开文件并重放。写路径用它 —— 会创建文件。"""
        key = scope.key()
        stream = self._streams.get(key)
        if stream is not None:
            return stream
        stream = _Stream(scope=scope)
        handle = _ScopeFile(self._root, scope)
        self._streams[key] = stream
        self._files[key] = handle
        try:
            self._replay(stream)
        except BaseException:
            # 重放失败不能把半成品留在缓存里：下一次调用会直接拿到它，把损坏当成
            # 一个正常的空流用，于是只在第一次调用时报错。
            del self._streams[key]
            del self._files[key]
            handle.close()
            raise
        return stream

    def _existing_stream(self, scope: Scope) -> _Stream | None:
        """取流；日志文件不存在返回 None。

        读路径用它 —— `head()` 对一个还没写过的 scope 必须返回全零，且不能因为
        「读了一下」就在磁盘上凭空造出目录和文件。
        """
        key = scope.key()
        if key in self._streams:
            return self._streams[key]
        if not os.path.exists(self._path_for(scope)):
            return None
        return self._stream(scope)

    def _replay(self, stream: _Stream) -> None:
        """从日志重建派生状态：commits / dedup / index 和三个计数器。

        只可能最后一行写了一半（进程被杀），中间行坏掉是真的损坏 —— 直接抛
        LogCorrupt 停下来，不猜测也不跳过。
        """
        handle = self._files[stream.scope.key()]
        for offset, obj in handle.read_all_lines():
            kind = obj.get("kind")
            if obj.get("v") != LINE_VERSION:
                raise LogCorrupt(f"未知的日志行版本 {obj.get('v')!r}")
            if kind == "lease":
                stream.owner_epoch = int(obj["owner_epoch"])
                stream.holder = obj.get("holder")
                continue
            if kind != "commit":
                raise LogCorrupt(f"未知的日志行 kind: {kind!r}")

            first_seq = int(obj["first_seq"])
            count = int(obj["record_count"])
            if first_seq != stream.head_seq + 1:
                raise LogCorrupt(
                    f"seq 不连续：期望 {stream.head_seq + 1}，实际 {first_seq}"
                    f"（commit {obj.get('commit_id')!r}）"
                )
            commit_id = str(obj["commit_id"])
            checksum = str(obj["checksum"])
            prior = stream.commits.get(commit_id)
            if prior is not None and prior.checksum != checksum:
                raise LogCorrupt(f"commit {commit_id!r} 在日志里出现两次且内容不同")

            for row in obj["records"]:
                record = JournalRecord.from_row(row, stream.scope)
                if record.seq < first_seq or record.seq >= first_seq + count:
                    raise LogCorrupt(f"记录 seq {record.seq} 落在 commit 区间之外")
                if record.event_id is not None:
                    stream.dedup[record.event_id] = (record.payload_hash, commit_id)

            stream.index.append((first_seq, count, offset))
            stream.commits[commit_id] = _StoredCommit(
                checksum=checksum,
                first_seq=first_seq,
                record_count=count,
                committed_at=from_iso(obj["committed_at"]),
                state_version=obj.get("state_version"),
                owner_epoch=obj.get("owner_epoch"),
            )
            stream.head_seq = first_seq + count - 1
            if obj.get("state_version") is not None:
                stream.state_version = int(obj["state_version"])
            if obj.get("owner_epoch") is not None:
                stream.owner_epoch = int(obj["owner_epoch"])

    def _heal(self, stream: _Stream) -> None:
        """文件比已知完整前缀长 = 上次崩溃留下了半行。截掉。"""
        handle = self._files[stream.scope.key()]
        if handle.size > handle.good_bytes:
            handle.truncate_to_good()

    # ---------- 绑定 ----------

    def bind(self, principal: Principal, scope: Scope, producer: str, *,
             allowed_types: frozenset[str] | None = None,
             allowed_categories: frozenset[str] | None = None) -> JsonlWriter:
        _authorize(principal, scope)
        if allowed_categories and "state" in allowed_categories:
            raise ValidationError("普通 writer 不能获得 category=state 的写权限")
        return JsonlWriter(self, principal, scope, producer, allowed_types, allowed_categories)

    def bind_state(self, principal: Principal, scope: Scope, producer: str, *,
                   owner_epoch: int, allowed_types: frozenset[str] | None = None,
                   ) -> JsonlStateWriter:
        _authorize(principal, scope)
        return JsonlStateWriter(self, principal, scope, producer, owner_epoch, allowed_types)

    # ---------- 核心提交路径 ----------

    async def _commit_locked(
        self,
        principal: Principal,
        scope: Scope,
        producer: str,
        records: Sequence[RecordDraft],
        commit_id: str,
        *,
        is_state: bool,
        expected_state_version: int | None,
        owner_epoch: int | None,
        allowed_types: frozenset[str] | None,
        allowed_categories: frozenset[str] | None,
    ) -> CommitReceipt:
        """append 与 commit_state 的唯一实现。调用方已持有 `self._lock`。

        顺序：校验 → commit 幂等 → event 去重 → 检查 epoch 与 state_version →
        分配 seq → 落盘 → 更新计数器。

        检查必须在分配 seq 之前，否则一次被拒的提交会在日志里留下 seq 空洞。
        """
        _authorize(principal, scope)
        drafts = tuple(records)
        issues = validate_batch(
            drafts, self._limits,
            allowed_types=allowed_types,
            allowed_categories=allowed_categories,
            require_state=is_state,
        )
        if issues:
            raise ValidationError("; ".join(str(issue) for issue in issues))
        if not drafts:
            raise ValidationError("提交不能包含空记录列表")

        # producer 强制覆盖：记录里的 producer 必须反映真正的写入模块，否则扩展
        # 可以伪装成 core 写终态（feature07）。
        normalized = tuple(replace(draft, producer=producer) for draft in drafts)
        batch = CommitBatch(commit_id=commit_id, scope=scope, records=normalized)
        checksum = checksum_of(batch.content_parts())

        async with self._lock:
            stream = self._stream(scope)
            self._heal(stream)

            # (1) commit 幂等：同 id 同内容 → 原 receipt；不同内容 → conflict。
            existing = stream.commits.get(commit_id)
            if existing is not None:
                if existing.checksum != checksum:
                    return CommitReceipt(
                        outcome="conflict", commit_id=commit_id, scope=scope,
                        detail="同 commit_id 但内容不同 —— 视为 corruption，不覆盖既有记录",
                    )
                return CommitReceipt(
                    outcome="duplicate", commit_id=commit_id, scope=scope,
                    first_seq=existing.first_seq, record_count=existing.record_count,
                    checksum=existing.checksum, committed_at=existing.committed_at,
                    state_version=existing.state_version, owner_epoch=existing.owner_epoch,
                )

            # (2) event_id 去重。
            for draft in normalized:
                if draft.event_id is None:
                    continue
                current = stream.dedup.get(draft.event_id)
                draft_hash = payload_hash_of(draft)
                if current is None:
                    continue
                prior_hash, prior_commit = current
                if prior_hash != draft_hash:
                    return CommitReceipt(
                        outcome="conflict", commit_id=commit_id, scope=scope,
                        detail=(f"event_id {draft.event_id!r} 已存在但内容不同 —— "
                                "feature01：相同 ID + 不同内容返回冲突"),
                    )
                prior = stream.commits[prior_commit]
                return CommitReceipt(
                    outcome="duplicate", commit_id=prior_commit, scope=scope,
                    first_seq=prior.first_seq, record_count=prior.record_count,
                    checksum=prior.checksum, committed_at=prior.committed_at,
                    state_version=prior.state_version, owner_epoch=prior.owner_epoch,
                    detail=f"event_id {draft.event_id!r} 已被 commit {prior_commit} 接收",
                )

            # (3) CAS —— 在分配 seq 之前，也在一行落盘之前。
            if is_state:
                if owner_epoch is None:
                    raise ValidationError("commit_state 必须提供 owner_epoch")
                if owner_epoch != stream.owner_epoch:
                    raise FencedOut(
                        f"owner_epoch {owner_epoch} 不是当前 epoch",
                        expected_epoch=owner_epoch, actual_epoch=stream.owner_epoch,
                    )
                if expected_state_version != stream.state_version:
                    raise StateVersionConflict(
                        f"expected_state_version {expected_state_version} "
                        f"与当前 {stream.state_version} 不符",
                        expected=expected_state_version or -1, actual=stream.state_version,
                    )

            # (4) 分配 seq 与时间戳。
            first_seq = stream.head_seq + 1
            committed_at = datetime.now(UTC)
            new_state_version = stream.state_version + 1 if is_state else None

            written = [
                JournalRecord(
                    seq=first_seq + offset,
                    record_id=draft.record_id,
                    scope=scope,
                    commit_id=commit_id,
                    type=draft.type,
                    category=draft.category,
                    producer=draft.producer,
                    payload_hash=payload_hash_of(draft),
                    occurred_at=draft.occurred_at if draft.occurred_at is not None
                    else committed_at,
                    committed_at=committed_at,
                    schema_version=draft.schema_version,
                    payload=dict(draft.payload) if draft.payload is not None else None,
                    payload_ref=draft.payload_ref,
                    run_id=draft.run_id,
                    step_id=draft.step_id,
                    correlation_id=draft.correlation_id,
                    causation_id=draft.causation_id,
                    queue=draft.queue,
                    event_id=draft.event_id,
                )
                for offset, draft in enumerate(normalized)
            ]

            # (5) 落盘。**先写后改内存** —— 顺序反过来的话，落盘失败会留下一个
            #     声称已提交、实际不存在的计数器状态。
            handle = self._files[scope.key()]
            envelope: dict[str, Any] = {
                "v": LINE_VERSION,
                "kind": "commit",
                "commit_id": commit_id,
                "checksum": checksum,
                "first_seq": first_seq,
                "record_count": len(written),
                "committed_at": to_iso(committed_at),
                "state_version": new_state_version,
                "owner_epoch": owner_epoch if is_state else None,
                "records": [record.to_row() for record in written],
            }
            offset = handle.good_bytes
            if self._crash_next_commit:
                self._crash_next_commit = False
                self._write_half_line(handle, envelope)
                raise InjectedCrash("注入的崩溃：commit 行只写了一半")
            handle.append_line(envelope)

            # (6) 计数器与索引推进。都在落盘成功之后。
            stream.index.append((first_seq, len(written), offset))
            stream.commits[commit_id] = _StoredCommit(
                checksum=checksum, first_seq=first_seq, record_count=len(written),
                committed_at=committed_at,
                state_version=new_state_version,
                owner_epoch=owner_epoch if is_state else None,
            )
            for draft in normalized:
                if draft.event_id is not None:
                    stream.dedup[draft.event_id] = (payload_hash_of(draft), commit_id)
            stream.head_seq = first_seq + len(written) - 1
            if is_state:
                # +1 而非 +len(records)：一次 state commit 是一个原子决定。
                stream.state_version = new_state_version or 0

            return CommitReceipt(
                outcome="accepted", commit_id=commit_id, scope=scope,
                first_seq=first_seq, record_count=len(written),
                checksum=checksum, committed_at=committed_at,
                state_version=new_state_version, owner_epoch=owner_epoch if is_state else None,
            )

    def _write_half_line(self, handle: _ScopeFile, envelope: dict[str, Any]) -> None:
        """写半行就停。不加换行、不 fsync 之后的任何修正。"""
        line = (canonical_json(envelope) + "\n").encode("utf-8")
        handle.write_raw(line[: len(line) // 2])

    # ---------- 租约 ----------

    def _append_lease(self, stream: _Stream, epoch: int, holder: str, at: datetime) -> None:
        self._files[stream.scope.key()].append_line({
            "v": LINE_VERSION,
            "kind": "lease",
            "owner_epoch": epoch,
            "holder": holder,
            "at": to_iso(at),
        })

    # ---------- 读取 ----------

    async def read(self, request: ReadRequest) -> ReadPage:
        _authorize(request.principal, request.scope)
        check_page_limit(request.limit, self._limits.max_page_size)
        async with self._lock:
            key = request.scope.key()
            stream = self._existing_stream(request.scope)
            if stream is None:
                return ReadPage(items=(), through_seq=request.after_seq, head_seq=0,
                                next_cursor=None)
            self._heal(stream)
            handle = self._files[key]
            items: list[JournalRecord] = []
            scanned = request.after_seq
            # 逐个 commit 定位。index 里的 (first_seq, count, offset) 让读取不必
            # 从头扫 —— 但被过滤掉的记录同样推进 through_seq。
            for first_seq, count, offset in stream.index:
                if first_seq + count - 1 <= request.after_seq:
                    continue
                line = handle.read_line_at(offset)
                for row in line["records"]:
                    record = JournalRecord.from_row(row, request.scope)
                    if record.seq <= request.after_seq:
                        continue
                    scanned = record.seq
                    if not request.filter.matches(record):
                        continue
                    items.append(record)
                    if len(items) >= request.limit:
                        break
                if len(items) >= request.limit:
                    break
            cursor: Cursor | None = None
            if scanned < stream.head_seq:
                cursor = Cursor.from_request(
                    request, through_seq=scanned, issued_at=datetime.now(UTC),
                )
            return ReadPage(
                items=tuple(items),
                through_seq=scanned,
                head_seq=stream.head_seq,
                next_cursor=self._cursors.seal(cursor) if cursor is not None else None,
            )

    async def head(self, principal: Principal, scope: Scope) -> Head:
        _authorize(principal, scope)
        async with self._lock:
            stream = self._existing_stream(scope)
            if stream is None:
                return Head(seq=0, state_version=0, owner_epoch=0)
            self._heal(stream)
            return Head(seq=stream.head_seq, state_version=stream.state_version,
                        owner_epoch=stream.owner_epoch)

    def unseal_cursor(self, token: str, request: ReadRequest) -> Cursor:
        return self._cursors.unseal(token, request)

    def close(self) -> None:
        for handle in self._files.values():
            handle.close()
        self._files.clear()
        self._streams.clear()


class JsonlWriter(Writer):
    """普通模块的追加能力。构造即固定 scope / producer / 白名单。"""

    def __init__(self, journal: JsonlJournal, principal: Principal, scope: Scope,
                 producer: str, allowed_types: frozenset[str] | None,
                 allowed_categories: frozenset[str] | None) -> None:
        self._journal = journal
        self._principal = principal
        self._scope = scope
        self._producer = producer
        self._allowed_types = allowed_types
        self._allowed_categories = allowed_categories

    @property
    def scope(self) -> Scope:
        return self._scope

    @property
    def producer(self) -> str:
        return self._producer

    async def append(self, records: Sequence[RecordDraft], commit_id: str) -> CommitReceipt:
        return await self._journal._commit_locked(
            self._principal, self._scope, self._producer, records, commit_id,
            is_state=False, expected_state_version=None, owner_epoch=None,
            allowed_types=self._allowed_types, allowed_categories=self._allowed_categories,
        )


class JsonlStateWriter(StateWriter):
    """执行状态提交能力。epoch 在构造时固定 —— writer 本身就是一张 fencing 凭证。

    只能写 category=state。诊断记录搭执行状态提交的便车会让「哪些记录参与恢复」
    与「哪些只是日志」在日志里失去可分辨性。
    """

    def __init__(self, journal: JsonlJournal, principal: Principal, scope: Scope,
                 producer: str, owner_epoch: int, allowed_types: frozenset[str] | None) -> None:
        self._journal = journal
        self._principal = principal
        self._scope = scope
        self._producer = producer
        self._owner_epoch = owner_epoch
        self._allowed_types = allowed_types

    @property
    def scope(self) -> Scope:
        return self._scope

    async def commit_state(self, records: Sequence[RecordDraft], commit_id: str, *,
                           expected_state_version: int, owner_epoch: int) -> CommitReceipt:
        if owner_epoch != self._owner_epoch:
            raise FencedOut(
                "writer 绑定的 owner_epoch 与调用参数不一致",
                expected_epoch=self._owner_epoch, actual_epoch=owner_epoch,
            )
        return await self._journal._commit_locked(
            self._principal, self._scope, self._producer, records, commit_id,
            is_state=True, expected_state_version=expected_state_version,
            owner_epoch=owner_epoch, allowed_types=self._allowed_types,
            allowed_categories=frozenset({"state"}),
        )


class JsonlLease(Lease):
    """租约。epoch 递增本身是一条日志行，所以重启后仍单调。"""

    def __init__(self, journal: JsonlJournal) -> None:
        self._journal = journal

    async def acquire(self, scope: Scope, holder: str) -> int:
        async with self._journal._lock:
            stream = self._journal._stream(scope)
            epoch = stream.owner_epoch + 1
            self._journal._append_lease(stream, epoch, holder, datetime.now(UTC))
            stream.owner_epoch = epoch
            stream.holder = holder
            return epoch

    async def renew(self, scope: Scope, owner_epoch: int) -> bool:
        async with self._journal._lock:
            stream = self._journal._streams.get(scope.key())
            return stream is not None and stream.owner_epoch == owner_epoch


def _authorize(principal: Principal, scope: Scope) -> None:
    """scope 检查在 journal 层执行，不依赖调用方自觉（feature06）。"""
    if not principal.can_access(scope):
        raise ScopeViolation(
            f"principal {principal.principal_id!r} 无权访问 "
            f"user={scope.user_id!r} session={scope.session_id!r}"
        )