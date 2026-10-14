"""Buffer 的 durable 保证来自 Journal 事务，不再单独 clear 三个文件。"""

from pathlib import Path
from typing import Any

import pytest

from core.buffer import JournalEventBuffer
from core.journal.impl import FileJournal
from core.journal.runtime_store import RuntimeStore
from core.protocol.datatype import Scope
from core.protocol.datatype.inbound import Inbound, InboundKind


def test_restart_preserves_receipt_and_timer_start(tmp_path: Path, scope: Scope) -> None:
    item = Inbound(InboundKind.USER, "user.message", {"content": "原文"}, "m1")
    with FileJournal(tmp_path) as journal:
        store = RuntimeStore(journal)
        buffer = JournalEventBuffer(store)
        buffer.receive(scope, item)
        before = store.read(scope)["inputs"]
    with FileJournal(tmp_path) as journal:
        store = RuntimeStore(journal)
        buffer = JournalEventBuffer(store)
        buffer.receive(scope, item)
        assert store.read(scope)["inputs"] == before
        assert buffer.snapshot(scope, "user") == (item,)


def test_failed_receive_keeps_accepted_input(
    journal: FileJournal, scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RuntimeStore(journal)
    buffer = JournalEventBuffer(store)
    item = Inbound(InboundKind.USER, "user.message", {"content": "原文"}, "m1")
    buffer.accept(scope, item)
    original = journal.append

    def fail(*args: Any, **kwargs: Any) -> Any:
        if args[2].get("reason") == "buffer.received":
            raise OSError("磁盘故障")
        return original(*args, **kwargs)

    monkeypatch.setattr(journal, "append", fail)
    with pytest.raises(OSError):
        buffer.receive(scope, item)
    assert buffer.unreceived(scope, "user") == (item,)
    assert not buffer.snapshot(scope, "user")
