from core.protocol.datatype import ToolDefinition, ToolResult
from core.protocol.interface import Tool
from core.tasks.collaboration_tools import tools as collaboration_tools
from core.subagents.tools import SubagentTool
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


def test_subagent_tools_are_never_exposed_to_the_interaction_agent():
    tool = SubagentTool(None, "spawn")
    assert tool.subagent_only
    assert tool.allow_subagent


def test_team_and_workflow_tools_are_delegated_only():
    values = collaboration_tools(None)
    assert {tool.definition.name for tool in values} == {
        "team_send",
        "team_inbox",
        "team_members",
        "workflow_define",
        "workflow_status",
    }
    assert all(tool.subagent_only and tool.allow_subagent for tool in values)
