from types import SimpleNamespace

from core.consumer import ToolConsumer
from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import QueueName
from core.protocol.datatype.user import User


class Store:
    def __init__(self, state):
        self.state = state

    def read(self, user):
        return self.state

    def transact(self, user, _kind, change):
        return change(self.state)


class Buffer:
    def __init__(self, user_items=()):
        self.items = {QueueName.USER: list(user_items), QueueName.TOOL: []}

    def add(self, user, queue, item):
        self.items[queue].append(item)

    def peek(self, user, queue):
        return list(self.items[queue])


class Sink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def terminal():
    return {
        "batch_id": "a" * 32,
        "event_id": "result:" + "a" * 32 + "-0",
        "invocation_id": "a" * 32 + "-0",
        "outcome": "succeeded",
        "run_id": "old-run",
        "tool_call_id": "call_1",
    }


def item(payload, occur_dt="2026-10-22T12:04:00Z"):
    return Inbound(
        kind=QueueName.TOOL,
        source="tool.terminal",
        payload=payload,
        source_id=payload["event_id"],
        occur_dt=occur_dt,
    )


def consumer(state, buffer):
    sink = Sink()
    ctx = SimpleNamespace(
        store=Store(state),
        buffer=buffer,
        sink=sink,
        config=SimpleNamespace(tool_round_timeout_seconds=10),
    )
    return ToolConsumer(ctx, None), sink


def test_late_tool_result_from_previous_run_enters_buffer():
    payload = terminal()
    state = {
        "meta": {"run_id": "new-run"},
        "calls": {payload["invocation_id"]: {"terminal": payload}},
        "batches": {payload["batch_id"]: {"buffer_deadline": None}},
    }
    buffer = Buffer()
    tool, sink = consumer(state, buffer)

    tool.receive(User("alice"), item(payload))

    assert buffer.items[QueueName.TOOL] == [item(payload)]
    assert sink.events == []


def test_tool_result_arriving_after_buffered_user_message_is_preserved():
    payload = terminal()
    user = Inbound(
        kind=QueueName.USER,
        source="user.message",
        payload={"content": "放弃吧哥"},
        source_id="user",
        occur_dt="2026-10-22T12:03:52Z",
    )
    buffer = Buffer([user])
    tool, sink = consumer(
        {
            "meta": {"run_id": "same-run"},
            "calls": {payload["invocation_id"]: {"terminal": payload}},
            "batches": {payload["batch_id"]: {"buffer_deadline": None}},
        },
        buffer,
    )

    tool.receive(User("alice"), item(payload, "2026-10-22T12:04:09Z"))

    assert buffer.items[QueueName.TOOL] == [item(payload, "2026-10-22T12:04:09Z")]
    assert sink.events == []
