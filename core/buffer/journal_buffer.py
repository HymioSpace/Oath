"""三个独立 buffer 的持久化接纳与 receipt；跨 buffer claim 由 Brain 原子提交。"""

from __future__ import annotations

import time
from typing import Any

from core.journal.runtime_store import RuntimeStore, State
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import QueueName, queue_of
from core.protocol.datatype.scope import Scope
from core.protocol.error import IdempotencyKeyConflict, UnknownQueueError
from core.protocol.interface.buffer import EventBuffer


def input_id(item: Inbound) -> str:
    if not item.source_id:
        raise ValueError("输入缺少稳定 source_id")
    queue_of(item.kind)
    return f"{item.kind}:{item.source_id}"


def accept_input(state: State, item: Inbound) -> bool:
    key = input_id(item)
    known = state["inputs"].get(key)
    if known is not None:
        original = Inbound.from_dict(known["item"])
        if (original.source, original.payload) != (item.source, item.payload):
            raise IdempotencyKeyConflict(f"输入 {key} 内容冲突")
        return False
    state["inputs"][key] = {
        "item": item.to_dict(),
        "queue": queue_of(item.kind),
        "phase": "accepted",
        "buffered_at": None,
        "step_id": None,
    }
    return True


def buffered(state: State, queue: str | None = None) -> list[tuple[str, Any]]:
    return sorted(
        (
            (key, entry)
            for key, entry in state["inputs"].items()
            if entry["phase"] == "buffered" and (queue is None or entry["queue"] == queue)
        ),
        key=lambda pair: pair[1]["buffer_seq"],
    )


class JournalEventBuffer(EventBuffer):
    def __init__(self, store: RuntimeStore, *, tool_buffer_timeout: float = 30) -> None:
        self.store = store
        self._tool_buffer_timeout = tool_buffer_timeout

    def accept(self, scope: Scope, item: Inbound) -> bool:
        return self.store.transact(scope, "input.accepted", lambda state: accept_input(state, item))

    def receive(self, scope: Scope, item: Inbound) -> None:
        def change(state: State) -> None:
            accept_input(state, item)
            entry = state["inputs"][input_id(item)]
            if entry["phase"] != "accepted":
                return
            seq = state["meta"].get("buffer_seq", 0) + 1
            state["meta"]["buffer_seq"] = seq
            now = time.time()
            entry.update(phase="buffered", buffered_at=now, buffer_seq=seq)
            if entry["queue"] == "tool":
                batch = state["batches"][item.payload["batch_id"]]
                if batch.get("buffer_deadline") is None:
                    batch.update(
                        first_buffered_at=now,
                        buffer_deadline=now + self._tool_buffer_timeout,
                        buffer_window=self._tool_buffer_timeout,
                    )

        self.store.transact(scope, "buffer.received", change)

    def snapshot(self, scope: Scope, queue: str) -> tuple[Inbound, ...]:
        if queue not in QueueName.ALL:
            raise UnknownQueueError(queue)
        return tuple(
            Inbound.from_dict(e["item"]) for _, e in buffered(self.store.read(scope), queue)
        )

    def unreceived(self, scope: Scope, queue: str) -> tuple[Inbound, ...]:
        if queue not in QueueName.ALL:
            raise UnknownQueueError(queue)
        state = self.store.read(scope)
        return tuple(
            Inbound.from_dict(e["item"])
            for e in state["inputs"].values()
            if e["queue"] == queue and e["phase"] == "accepted"
        )

    def scopes(self) -> tuple[Scope, ...]:
        return self.store.scopes()
