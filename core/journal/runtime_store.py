"""版本化状态提交。回调只能变换数据；跨进程冲突重读后重算。"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from collections.abc import Callable
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
    def __init__(self, storage: StoragePort, *, fence=None):
        self.storage = storage
        self.fence = fence
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

    def read(self, user: User) -> State:
        return cast(
            State, self.storage.call("state.read", user_id=user.storage_id, compact=True)["state"]
        )

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
            {"version": cached[0], "state": cached[1]}
            if cached
            else cast(
                Snapshot,
                self.storage.call("state.read", user_id=user.storage_id, compact=True),
            )
        )
        for attempt in range(16):
            after = copy.deepcopy(snapshot["state"])
            result = change(after)
            if after == snapshot["state"]:
                return copy.deepcopy(result)
            try:
                self.storage.call(
                    "state.commit",
                    user_id=user.storage_id,
                    expected=snapshot["version"],
                    state=after,
                    reason=reason,
                    commit_id=uuid.uuid4().hex,
                    fence=self._context_fence.get() or self.fence,
                )
                self._snapshots.values[user.storage_id] = (
                    snapshot["version"] + 1,
                    after,
                )
                return copy.deepcopy(result)
            except StorageConflict:
                self._snapshots.values.pop(user.storage_id, None)
                time.sleep(min(0.005 * (attempt + 1), 0.05))
                snapshot = cast(
                    Snapshot,
                    self.storage.call("state.read", user_id=user.storage_id, compact=True),
                )
            except BaseException:
                self._snapshots.values.pop(user.storage_id, None)
                raise
        raise StorageConflict("State remained contended; retry the durable work item")

    def users(self):
        return tuple(User.from_storage_id(uid) for uid in self.storage.call("users.list"))
