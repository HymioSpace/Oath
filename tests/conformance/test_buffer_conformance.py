"""EventBuffer 的接纳、receipt 和 scope 隔离契约。"""

import pytest

from core.buffer import JournalEventBuffer
from core.journal.impl import FileJournal
from core.journal.runtime_store import RuntimeStore
from core.protocol.datatype import Scope
from core.protocol.datatype.inbound import Inbound, InboundKind
from core.protocol.error import IdempotencyKeyConflict, UnknownQueueError


@pytest.fixture
def buffer(journal: FileJournal) -> JournalEventBuffer:
    return JournalEventBuffer(RuntimeStore(journal))


def message(id: str = "m1", content: str = "内容") -> Inbound:
    return Inbound(InboundKind.USER, "user.message", {"content": content}, id)


def test_accept_is_not_yet_buffered(buffer: JournalEventBuffer, scope: Scope) -> None:
    assert buffer.accept(scope, message())
    assert not buffer.snapshot(scope, "user")
    assert buffer.unreceived(scope, "user") == (message(),)


def test_receive_routes_to_own_queue(buffer: JournalEventBuffer, scope: Scope) -> None:
    buffer.receive(scope, message())
    assert buffer.snapshot(scope, "user") == (message(),)
    assert not buffer.snapshot(scope, "tool")
    assert not buffer.snapshot(scope, "system")
    assert not buffer.unreceived(scope, "user")


def test_duplicate_receipt_is_idempotent(buffer: JournalEventBuffer, scope: Scope) -> None:
    buffer.receive(scope, message())
    buffer.receive(scope, message())
    assert buffer.snapshot(scope, "user") == (message(),)


def test_same_id_different_content_is_conflict(buffer: JournalEventBuffer, scope: Scope) -> None:
    buffer.accept(scope, message())
    with pytest.raises(IdempotencyKeyConflict):
        buffer.receive(scope, message(content="另一份"))


def test_same_session_different_user_is_isolated(buffer: JournalEventBuffer, scope: Scope) -> None:
    buffer.receive(scope, message())
    other = Scope("other", scope.session_id)
    assert not buffer.snapshot(other, "user")
    buffer.receive(other, message(content="不同用户"))
    assert buffer.snapshot(scope, "user")[0].payload["content"] == "内容"


def test_snapshot_is_detached(buffer: JournalEventBuffer, scope: Scope) -> None:
    buffer.receive(scope, message())
    buffer.snapshot(scope, "user")[0].payload["content"] = "外部修改"
    assert buffer.snapshot(scope, "user")[0].payload["content"] == "内容"


def test_unknown_queue_rejected(buffer: JournalEventBuffer, scope: Scope) -> None:
    with pytest.raises(UnknownQueueError):
        buffer.snapshot(scope, "unknown")


def test_stable_id_required(buffer: JournalEventBuffer, scope: Scope) -> None:
    with pytest.raises(ValueError):
        buffer.accept(scope, message(id=""))
