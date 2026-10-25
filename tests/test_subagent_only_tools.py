from core.protocol.datatype import ToolDefinition, ToolResult
from core.protocol.interface import Tool
from core.tool.registry import ToolRegistry


class CodingTool(Tool):
    subagent_only = True

    @property
    def definition(self):
        return ToolDefinition(
            "coding_tool",
            "Only a delegated coding context can see this tool.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        )

    def execute(self, invocation):
        return ToolResult({"ok": True})


def test_subagent_only_tool_is_hidden_from_main_context():
    registry = ToolRegistry(default_timeout_seconds=30)
    registry.register(CodingTool())

    assert registry.definitions(channel="ios") == ()
    assert [tool.name for tool in registry.definitions(delegated=True, channel="ios")] == [
        "coding_tool"
    ]
