"""终态与接纳记录同一次提交，通知失败不能改写已知结果。"""

from pathlib import Path

from tests._support.intake_fixtures import Harness, requests, until
from tests._support.tool_fixtures import EchoTool


def test_outbound_failure_does_not_lose_terminal_or_change_success(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")]) as h:
        h.sink.fail = True
        h.tools.execute(h.scope, requests("a"))
        until(lambda: h.call("a")["terminal"])
        until(lambda: h.queue.pending(h.scope, "tool"))
        assert h.call("a")["status"] == "succeeded"
        assert any(e["queue"] == "tool" for e in h.state()["inputs"].values())


def test_lost_queue_notification_is_recovered_from_accepted_input(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")]) as h:
        h.tools.execute(h.scope, requests("a"))
        until(lambda: h.call("a")["terminal"])
        h.queue.drain(h.scope, "tool")
        h.consumers[1].consume(h.scope)
        assert len(h.buffers.snapshot(h.scope, "tool")) == 1


def test_duplicate_terminal_notification_does_not_duplicate_buffer(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")]) as h:
        h.tools.execute(h.scope, requests("a"))
        until(lambda: h.call("a")["terminal"])
        h.consumers[1].consume(h.scope)
        item = h.buffers.snapshot(h.scope, "tool")[0]
        h.queue.put(h.scope, item)
        h.consumers[1].consume(h.scope)
        assert h.buffers.snapshot(h.scope, "tool") == (item,)
