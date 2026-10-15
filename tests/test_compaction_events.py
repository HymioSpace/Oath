"""压缩在线事件与状态持久化的先后关系。"""

import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from biz.api import AgentApi
from bootstrap.runtime import Runtime
from core.compaction import CompactionCoordinator
from core.protocol.datatype.config import OathConfig
from core.protocol.datatype.scope import Scope
from tests._support.intake_fixtures import ScriptedProvider, until
from tests._support.tool_fixtures import RecordingSink
from tests.test_context_compaction import CountingCompactor, build, config, history, seed
from tests.test_runtime_intake_wire import Connection


def statuses(sink: RecordingSink) -> list[str]:
    return [str(e.payload["status"]) for e in sink.events if e.type == "context.compaction"]


def test_async_ready_does_not_mean_applied(tmp_path: Path) -> None:
    journal, store, _ = build(tmp_path, CountingCompactor())
    sink = RecordingSink()
    coordinator = CompactionCoordinator(store, CountingCompactor(), config(), sink=sink)
    scope = Scope("u1", "s1")
    try:
        seed(store, history(6))
        coordinator.prepare(scope)
        until(lambda: "ready" in statuses(sink))
        assert statuses(sink) == ["started", "ready"]
        assert "applied" not in store.read(scope)["compactions"]
        ready = sink.events[-1].payload
        coordinator._apply_pending(scope)
        assert statuses(sink) == ["started", "ready", "applied"]
        assert sink.events[-1].payload["compression_id"] == ready["compression_id"]
        assert (
            store.read(scope)["compactions"]["applied"]["compression_id"] == ready["compression_id"]
        )
    finally:
        coordinator.shutdown()
        journal.close()


def test_failed_and_unusable_summary_have_terminal_notifications(tmp_path: Path) -> None:
    compactor = CountingCompactor()
    journal, store, _ = build(tmp_path, compactor)
    sink = RecordingSink()
    coordinator = CompactionCoordinator(store, compactor, config(), sink=sink)
    scope = Scope("u1", "s1")
    try:
        seed(store, history(10))
        with patch.object(compactor, "compress", side_effect=RuntimeError("synthetic")):
            coordinator._compress(scope, applied=True)
        assert statuses(sink) == ["started", "failed"]
        with patch.object(compactor, "compress", return_value=None):
            coordinator._compress(scope, applied=True)
        assert statuses(sink)[-2:] == ["started", "skipped"]
        assert not store.read(scope)["compactions"]
    finally:
        coordinator.shutdown()
        journal.close()


def test_sink_failure_does_not_discard_saved_summary(tmp_path: Path) -> None:
    journal, store, _ = build(tmp_path, CountingCompactor())
    sink = RecordingSink()
    coordinator = CompactionCoordinator(store, CountingCompactor(), config(), sink=sink)
    try:
        seed(store, history(10))
        with patch.object(sink, "emit", side_effect=RuntimeError("disconnected")):
            coordinator._compress(Scope("u1", "s1"), applied=True)
        assert "applied" in store.read(Scope("u1", "s1"))["compactions"]
    finally:
        coordinator.shutdown()
        journal.close()


async def test_runtime_pushes_compaction_over_client_channel(tmp_path: Path) -> None:
    runtime = Runtime(
        OathConfig(data_root=tmp_path, agent=config(), credentials={"test": ("u1", "s1")}),
        loop=asyncio.get_running_loop(),
        provider=ScriptedProvider(),
    )
    connection = Connection()
    handler = connection.serve(runtime)
    try:
        await connection.command("session.open", "open", {"credential": "test"})
        await connection.wait_for(lambda f: f["type"] == "ack")
        seed(runtime.ctx.store, history(10))
        with patch.object(
            runtime.compaction._compactor,
            "compress",
            return_value=[{"role": "user", "content": "摘要"}],
        ):
            await connection.command("user.message", "u", {"content": "continue"})
            frame = await connection.wait_for(
                lambda f: (
                    f.get("event_type") == "context.compaction"
                    and f["payload"]["status"] == "applied"
                )
            )
        assert frame is not None
        assert frame["payload"]["mode"] == "sync"
        assert frame["session_id"] == "s1"
        saved = cast(dict[str, Any], AgentApi(runtime).status(Scope("u1", "s1"))["compaction"])
        assert saved["applied"]["compression_id"] == frame["payload"]["compression_id"]
    finally:
        await connection.close()
        await handler
        await asyncio.to_thread(runtime.close)
