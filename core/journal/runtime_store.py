"""版本化状态提交。回调只能变换数据；跨进程冲突重读后重算。"""

from __future__ import annotations

import copy
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

from core.protocol.datatype.user import User
from core.protocol.interface.storage import StorageConflict, StoragePort

State = dict[str, Any]
T = TypeVar("T")
TABLES = ("inputs", "batches", "calls", "steps", "meta", "conversation", "controls", "compactions")


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

    @contextmanager
    def fenced(self, fence):
        token = self._context_fence.set(fence)
        try:
            yield
        finally:
            self._context_fence.reset(token)

    def read(self, user: User) -> State:
        return self.storage.call("state.read", user_id=user.user_id)["state"]

    def transact(self, user: User, reason: str, change: Callable[[State], T]) -> T:
        for attempt in range(16):
            snapshot = self.storage.call("state.read", user_id=user.user_id)
            after = copy.deepcopy(snapshot["state"])
            result = change(after)
            if after == snapshot["state"]:
                return copy.deepcopy(result)
            try:
                self.storage.call(
                    "state.commit",
                    user_id=user.user_id,
                    expected=snapshot["version"],
                    state=after,
                    reason=reason,
                    commit_id=uuid.uuid4().hex,
                    fence=self._context_fence.get() or self.fence,
                )
                return copy.deepcopy(result)
            except StorageConflict:
                time.sleep(min(0.005 * (attempt + 1), 0.05))
        raise StorageConflict("State remained contended; retry the durable work item")

    def users(self):
        return tuple(User(uid) for uid in self.storage.call("users.list"))
