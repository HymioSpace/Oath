"""Scope 内的原子提交。模块共用一份 store，事务里只改数据，不做外部 I/O。

一条 runtime.commit 同时提交所有表的变化；append 成功之后才发布内存状态。
Journal 是唯一持久化来源。这里只提供事务，不决定消费、唤醒或执行策略。
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from typing import Any, TypeVar, cast

from core.protocol.datatype import Cursor, CursorKind, EventType, Scope
from core.protocol.interface.journal import Journal
from core.utils.encoding import JsonObject

State = dict[str, Any]
T = TypeVar("T")
TABLES = ("inputs", "batches", "calls", "steps", "meta", "conversation", "controls")


def initial_state() -> State:
    return {name: {} for name in TABLES}


def fold_commit(state: State, payload: State) -> None:
    for table, changes in payload["changes"].items():
        for key, value in changes.items():
            state[table][key] = value


class RuntimeStore:
    """单进程事务层；共享 Journal 实例与 store，不能同 scope 装配多个 writer。"""

    def __init__(self, journal: Journal) -> None:
        self.journal = journal
        self._guard = threading.Lock()
        self._locks: dict[Scope, threading.RLock] = {}
        self._states: dict[Scope, State] = {}
        self._versions: dict[Scope, int] = {}

    def _lock(self, scope: Scope) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(scope, threading.RLock())

    def _load(self, scope: Scope) -> State:
        if scope not in self._states:
            state = initial_state()
            version = 0
            for record in self.journal.read_range(scope, Cursor(CursorKind.LINE, 0)):
                if record.type == EventType.RUNTIME_COMMIT:
                    if record.payload["version"] != version + 1:
                        raise ValueError("runtime.commit version 不连续，停止恢复")
                    fold_commit(state, record.payload)
                    version += 1
            self._states[scope] = state
            self._versions[scope] = version
        return self._states[scope]

    def read(self, scope: Scope) -> State:
        with self._lock(scope):
            return copy.deepcopy(self._load(scope))

    def transact(self, scope: Scope, reason: str, change: Callable[[State], T]) -> T:
        with self._lock(scope):
            before = self._load(scope)
            after = copy.deepcopy(before)
            result = change(after)
            if set(after) != set(TABLES) or any(
                not isinstance(after[t], dict) or not before[t].keys() <= after[t].keys()
                for t in TABLES
            ):
                raise ValueError("事务不可删除 table / key；用状态标记完成")
            changes = {
                table: {
                    k: v
                    for k, v in after[table].items()
                    if k not in before[table] or before[table][k] != v
                }
                for table in TABLES
            }
            changes = {table: values for table, values in changes.items() if values}
            if changes:
                version = self._versions[scope] + 1
                try:
                    self.journal.append(
                        scope,
                        EventType.RUNTIME_COMMIT,
                        cast(
                            JsonObject, {"version": version, "reason": reason, "changes": changes}
                        ),
                        payload_schema_version=1,
                        idempotency_key=f"runtime:{version}",
                    )
                except BaseException:
                    # append 可能在 fsync 后失败；下一次重新读取，不能沿用猜测的旧版本。
                    self._states.pop(scope, None)
                    raise
                self._states[scope] = after
                self._versions[scope] = version
            return copy.deepcopy(result)

    def scopes(self) -> tuple[Scope, ...]:
        return self.journal.scopes()
