"""工具终态怎么走到模型面前。

**队列只是通知。** 终态先落 journal（`calls[...]["terminal"]`），再投进 tool 队列；
投给在线客户端失败不改写任何已知结果。模型看见的是 consumer 放进 buffer 的那条。
"""

from pathlib import Path

from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.queue import QueueName
from tests._support.intake_fixtures import Harness, requests, until
from tests._support.tool_fixtures import EchoTool


def test_a_failed_client_notification_does_not_change_the_result(tmp_path: Path) -> None:
    """在线客户端断了只让它晚一点知道，不能把已经跑完的工具说成没跑完。"""
    with Harness(tmp_path, tools=[EchoTool("a")]) as h:
        h.sink.fail = True

        h.tools.execute(h.scope, requests("a"))
        until(lambda: h.call("a")["terminal"])

        assert h.call("a")["status"] == "succeeded"
        assert not h.sink.events


def test_the_terminal_reaches_the_buffer_as_one_input(tmp_path: Path) -> None:
    with Harness(tmp_path, tools=[EchoTool("a")]) as h:
        h.tools.execute(h.scope, requests("a"))
        until(lambda: h.call("a")["terminal"])

        h.consumers[1].deliver(h.scope)
        h.consumers[1].deliver(h.scope)  # 重投：crash 重启或 ACK 后才崩都可能发生

        buffered = h.buffer.get_all(QueueName.TOOL)
        assert [item.source_id for item in buffered] == ["result:a"]
        # 通知本身不进 journal —— 只有 Brain 认领之后才是事实。
        assert h.state()["inputs"] == {}


def test_a_terminal_for_an_unknown_call_is_quarantined(tmp_path: Path) -> None:
    """队列通知不能凭空造出一个工具结果 —— 那会直接进模型的上下文。"""
    with Harness(tmp_path, tools=[EchoTool("a")]) as h:
        h.tools.execute(h.scope, requests("a"))
        until(lambda: h.call("a")["terminal"])

        h.submit(Inbound(QueueName.TOOL, "tool.terminal", {"invocation_id": "ghost"}, "ghost"))
        h.consumers[1].deliver(h.scope)

        assert [item.source_id for item in h.buffer.get_all(QueueName.TOOL)] == ["result:a"]
