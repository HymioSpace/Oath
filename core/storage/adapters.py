"""核心接口到 StoragePort 的适配；这里不打开 SQLite 文件。"""

from __future__ import annotations

import uuid

from core.journal.messages import project
from core.protocol.datatype import (
    AgentState,
    AppendResult,
    Cursor,
    CursorKind,
    Record,
    Recovery,
    StateRef,
    User,
)
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.trace import Span
from core.protocol.interface.buffer import EventBuffer
from core.protocol.interface.detail import DetailStore
from core.protocol.interface.event import AgentEventSink
from core.protocol.interface.journal import Journal
from core.protocol.interface.queue import EventQueue
from core.protocol.interface.trace import TraceStore
from core.utils.encoding import utc_now_iso


class StoredBuffer(EventBuffer):
    def __init__(self, storage, fence=None):
        self.storage, self.fence = storage, fence

    def add(self, user, queue, item):
        self.storage.call("buffer.add", user_id=user.user_id, item=item.to_dict(), fence=self.fence)

    def peek(self, user, queue):
        return tuple(
            Inbound.from_dict(r)
            for r in self.storage.call("buffer.peek", user_id=user.user_id, kind=queue)
        )

    def remove(self, user, queue, item):
        self.storage.call(
            "buffer.remove", user_id=user.user_id, identity=item.identity, fence=self.fence
        )

    def close(self):
        pass


class StoredQueue(EventQueue):
    def __init__(self, queue, fence=None):
        self.queue, self.fence = queue, fence

    def add(self, user, item):
        self.queue.call("queue.add", user_id=user.user_id, item=item.to_dict())


class DurableEventSink(AgentEventSink):
    def __init__(self, storage, fence=None):
        self.storage, self.fence = storage, fence

    def emit(self, event):
        self.storage.call(
            "outbox.append",
            user_id=event.user.user_id,
            type=event.type,
            payload=event.payload,
            key=event.idempotency_key or uuid.uuid4().hex,
            occur_dt=event.occur_dt,
            fence=self.fence,
        )


class StoredDetails(DetailStore):
    def __init__(self, storage):
        self.storage = storage

    def put(self, user, key, content):
        self.storage.call("detail.put", user_id=user.user_id, key=key, content=content)

    def get(self, user, key):
        return self.storage.call("detail.get", user_id=user.user_id, key=key)


class StoredTraces(TraceStore):
    def __init__(self, storage):
        self.storage = storage

    def append(self, user, span):
        self.storage.call("trace.append", user_id=user.user_id, span=span.to_dict())

    def trace_ids(self, user):
        return tuple(self.storage.call("trace.list", user_id=user.user_id))

    def spans(self, user, trace_id):
        return tuple(
            Span.from_dict(s)
            for s in self.storage.call("trace.get", user_id=user.user_id, trace_id=trace_id)
        )


class StoredJournal(Journal):
    def __init__(self, storage):
        self.storage = storage

    def users(self):
        return tuple(User(uid) for uid in self.storage.call("users.list"))

    def append(
        self, user, type, payload, *, payload_schema_version, idempotency_key, occur_dt=None
    ):
        if payload_schema_version != 1:
            raise ValueError("Unsupported journal schema")
        result = self.storage.call(
            "journal.append",
            user_id=user.user_id,
            type=type,
            payload=payload,
            key=idempotency_key,
            occur_dt=occur_dt,
        )
        return AppendResult(Cursor(CursorKind.ID, result["seq"]), result["seq"], result["hit"])

    def read_range(self, user, cursor, *, limit=None):
        if cursor.kind != CursorKind.ID:
            raise ValueError("SQLite journal requires an id cursor")
        for row in self.storage.call(
            "journal.read", user_id=user.user_id, after=cursor.value, limit=limit
        ):
            yield Record.of(Cursor(CursorKind.ID, row["seq"]), row["raw"])

    def commit_state(self, state):
        body = {
            "payload": state.payload,
            "cursor_start": state.cursor_start.to_json() if state.cursor_start else None,
            "cursor_end": state.cursor_end.to_json(),
            "payload_schema_version": state.payload_schema_version,
            "commit_dt": utc_now_iso(),
        }
        self.storage.call("snapshot.put", user_id=state.user.user_id, body=body)
        return StateRef(state.cursor_end, body["commit_dt"])

    def recover(self, user):
        raw = self.storage.call("snapshot.get", user_id=user.user_id)
        state = AgentState(user, None, Cursor(CursorKind.ID, 0))
        if raw:
            body = raw["body"]
            state = AgentState(
                user,
                Cursor.from_json(body["cursor_start"]) if body["cursor_start"] else None,
                Cursor.from_json(body["cursor_end"]),
                body["payload"],
                body["payload_schema_version"],
            )
        records = list(self.read_range(user, Cursor(CursorKind.ID, 0)))
        return Recovery(
            state,
            project(records, state.cursor_end),
            [r for r in records if r.cursor.value > state.cursor_end.value],
        )

    def close(self):
        pass
