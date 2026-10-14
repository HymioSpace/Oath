"""审批与执行共享的事务契约。"""

import threading
from typing import Any

from core.journal.impl import FileJournal
from core.journal.runtime_store import RuntimeStore, State
from core.protocol.datatype import Scope


def test_transactions_serialize_concurrent_scope_updates(
    journal: FileJournal, scope: Scope
) -> None:
    store = RuntimeStore(journal)
    barrier = threading.Barrier(5)

    def increment() -> None:
        barrier.wait(3)
        for _ in range(8):

            def change(state: State) -> None:
                state["meta"]["count"] = state["meta"].get("count", 0) + 1

            store.transact(scope, "increment", change)

    threads = [threading.Thread(target=increment) for _ in range(4)]
    for t in threads:
        t.start()
    barrier.wait(3)
    for t in threads:
        t.join(3)
    assert store.read(scope)["meta"]["count"] == 32
    assert RuntimeStore(journal).read(scope) == store.read(scope)


def test_transaction_result_and_read_are_detached(journal: FileJournal, scope: Scope) -> None:
    store = RuntimeStore(journal)

    def change(state: State) -> Any:
        state["controls"]["approval"] = {"applied": True}
        return state["controls"]["approval"]

    result = store.transact(scope, "decision", change)
    result["applied"] = False
    assert store.read(scope)["controls"]["approval"]["applied"] is True
