"""版本化状态提交。回调只能变换数据；跨进程冲突重读后重算。"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypedDict, TypeVar, cast

from core.protocol.datatype.user import User
from core.protocol.interface.storage import StorageConflict, StoragePort

State = dict[str, Any]
T = TypeVar("T")
TABLES = ("inputs", "batches", "calls", "steps", "meta", "conversation", "controls", "compactions")


class Snapshot(TypedDict):
    version: int
    state: State


class _ThreadSnapshots(threading.local):
    def __init__(self) -> None:
        self.values: dict[str, tuple[int, State]] = {}


def initial_state():
    return {name: {} for name in TABLES}


def fold_commit(state, payload):
    for table, changes in payload["changes"].items():
        state[table].update(changes)


class RuntimeStore:
    def __init__(
        self, storage: StoragePort, *, fence=None, patch_commits=False, read_projection=None
    ):
        if read_projection and not patch_commits:
            raise ValueError("Projected reads require patch commits")
        self.storage = storage
        self.fence = fence
        self.patch_commits = patch_commits
        self.read_projection = read_projection
        self._context_fence = ContextVar("transaction_fence", default=None)
        # A Brain executes its step transitions on one worker thread. Reusing the
        # snapshot returned by its previous commit avoids repeatedly transferring
        # the complete user state over RPC. Any concurrent writer still surfaces
        # as a version conflict and causes a fresh read.
        self._snapshots = _ThreadSnapshots()

    @contextmanager
    def fenced(self, fence):
        token = self._context_fence.set(fence)
        try:
            yield
        finally:
            self._context_fence.reset(token)

    def _read_snapshot(self, user: User, *, tables: Sequence[str] | None = None) -> Snapshot:
        options = (
            {"tables": list(tables)}
            if tables
            else ({"projection": self.read_projection} if self.read_projection else {})
        )
        return cast(
            Snapshot,
            self.storage.call("state.read", user_id=user.storage_id, compact=True, **options),
        )

    def read(self, user: User, *, tables: Sequence[str] | None = None) -> State:
        return self._read_snapshot(user, tables=tables)["state"]

    def entries(self, user: User, table: str, keys: list[str]) -> State:
        if len(keys) > 128:
            return {
                key: value
                for offset in range(0, len(keys), 128)
                for key, value in self.entries(user, table, keys[offset : offset + 128]).items()
            }
        if not self.patch_commits:
            state = self.read(user)[table]
            return {key: state[key] for key in keys if key in state}
        return self.storage.call("state.entries", user_id=user.storage_id, table=table, keys=keys)

    def transact(
        self,
        user: User,
        reason: str,
        change: Callable[[State], T],
        *,
        reuse_snapshot: bool = False,
    ) -> T:
        cached = self._snapshots.values.get(user.storage_id) if reuse_snapshot else None
        snapshot: Snapshot = (
            {"version": cached[0], "state": cached[1]} if cached else self._read_snapshot(user)
        )
        for attempt in range(16):
            after = copy.deepcopy(snapshot["state"])
            result = change(after)
            if after == snapshot["state"]:
                return copy.deepcopy(result)
            try:
                if set(after) != set(TABLES) or any(
                    not isinstance(after[table], dict)
                    or not snapshot["state"][table].keys() <= after[table].keys()
                    for table in TABLES
                ):
                    raise ValueError("State tables and keys cannot be deleted")
                transition = (
                    {
                        "changes": {
                            table: {
                                key: value
                                for key, value in after[table].items()
                                if key not in snapshot["state"][table]
                                or value != snapshot["state"][table][key]
                            }
                            for table in TABLES
                        }
                    }
                    if self.patch_commits
                    else {"state": after}
                )
                if self.read_projection == "tools" and any(
                    transition.get("changes", {}).get(table)
                    for table in ("steps", "inputs", "conversation", "compactions")
                ):
                    raise ValueError("Tool projection cannot modify execution history")
                receipt = self.storage.call(
                    "state.patch" if self.patch_commits else "state.commit",
                    user_id=user.storage_id,
                    expected=snapshot["version"],
                    **transition,
                    reason=reason,
                    commit_id=uuid.uuid4().hex,
                    fence=self._context_fence.get() or self.fence,
                )
                version = snapshot["version"] + 1
                if self.patch_commits:
                    if receipt["replayed"]:
                        fresh = self._read_snapshot(user)
                        self._snapshots.values[user.storage_id] = (fresh["version"], fresh["state"])
                        return copy.deepcopy(result)
                    version = receipt["version"]
                    for step_id, digest in receipt["archived_requests"].items():
                        after["steps"][step_id].update(request=None, request_archive=digest)
                    for step_id, digest in receipt.get("archived_drafts", {}).items():
                        if step_id in after["steps"]:
                            after["steps"][step_id].update(
                                request_draft=None, request_draft_archive=digest
                            )
                self._snapshots.values[user.storage_id] = (
                    version,
                    after,
                )
                return copy.deepcopy(result)
            except StorageConflict:
                self._snapshots.values.pop(user.storage_id, None)
                time.sleep(min(0.005 * (attempt + 1), 0.05))
                snapshot = self._read_snapshot(user)
            except BaseException:
                self._snapshots.values.pop(user.storage_id, None)
                raise
        raise StorageConflict("State remained contended; retry the durable work item")

    def users(self):
        return tuple(User.from_storage_id(uid) for uid in self.storage.call("users.list"))
