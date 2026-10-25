import copy

from core.journal.runtime_store import RuntimeStore, initial_state
from core.protocol.datatype.user import User
from core.protocol.interface.storage import StorageConflict


class SnapshotStorage:
    def __init__(self):
        self.version = 1
        self.state = initial_state()
        self.reads = 0
        self.commits = 0

    def call(self, operation, **params):
        if operation == "state.read":
            self.reads += 1
            return {"version": self.version, "state": copy.deepcopy(self.state)}
        if operation == "state.commit":
            self.commits += 1
            if params["expected"] != self.version:
                raise StorageConflict("stale")
            self.state = copy.deepcopy(params["state"])
            self.version += 1
            return True
        raise AssertionError(operation)


def test_reused_snapshot_avoids_repeated_full_state_reads():
    storage = SnapshotStorage()
    store = RuntimeStore(storage)
    user = User("snapshot")

    store.transact(
        user,
        "first",
        lambda state: state["meta"].update(first=True),
        reuse_snapshot=True,
    )
    store.transact(
        user,
        "second",
        lambda state: state["meta"].update(second=True),
        reuse_snapshot=True,
    )

    assert storage.reads == 1
    assert storage.commits == 2
    assert storage.state["meta"] == {"first": True, "second": True}


def test_conflict_discards_cached_snapshot_and_recomputes():
    storage = SnapshotStorage()
    store = RuntimeStore(storage)
    user = User("snapshot")
    store.transact(
        user,
        "seed",
        lambda state: state["meta"].update(count=0),
        reuse_snapshot=True,
    )

    storage.state["meta"]["external"] = True
    storage.version += 1
    store.transact(
        user,
        "increment",
        lambda state: state["meta"].update(count=state["meta"]["count"] + 1),
        reuse_snapshot=True,
    )

    assert storage.reads == 2
    assert storage.commits == 3
    assert storage.state["meta"] == {"count": 1, "external": True}


def test_intermediate_writer_refreshes_snapshot_for_the_next_step_transaction():
    storage = SnapshotStorage()
    store = RuntimeStore(storage)
    user = User("snapshot")
    store.transact(
        user,
        "seed",
        lambda state: state["meta"].update(step="request"),
        reuse_snapshot=True,
    )
    store.transact(
        user,
        "tool.batch_declared",
        lambda state: state["meta"].update(tool=True),
    )
    store.transact(
        user,
        "step.completed",
        lambda state: state["meta"].update(step="completed"),
        reuse_snapshot=True,
    )

    assert storage.reads == 2
    assert storage.commits == 3
    assert storage.state["meta"] == {"step": "completed", "tool": True}
