from pathlib import Path

from core.protocol.datatype import ToolDefinition, ToolInvocation, ToolResult, User
from core.protocol.interface import Tool
from core.skill.system import SkillSystem
from core.tool.registry import ToolRegistry


SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


class ScopedTool(Tool):
    expose_channels = frozenset({"ios"})

    @property
    def definition(self):
        return ToolDefinition("scoped", "scoped tool", SCHEMA)

    def execute(self, invocation):
        return ToolResult({"channel": invocation.channel})


class OpenTool(Tool):
    @property
    def definition(self):
        return ToolDefinition("open", "open tool", SCHEMA)

    def execute(self, invocation):
        return ToolResult()


def test_tools_can_be_scoped_to_client_channels():
    registry = ToolRegistry()
    registry.register(ScopedTool())
    registry.register(OpenTool())

    assert registry.definitions(channel="ios") == (
        OpenTool().definition,
        ScopedTool().definition,
    )
    assert registry.definitions(channel="web") == (OpenTool().definition,)
    assert registry.definitions() == (OpenTool().definition, ScopedTool().definition)


def test_skill_index_search_and_read_follow_the_same_channel_scope(tmp_path):
    skill = tmp_path / "ios-only"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: ios-only\n"
        "description: create a mobile application\n"
        "metadata:\n"
        "  channels: [ios]\n"
        "---\n"
        "Only on iOS.\n"
    )
    skills = SkillSystem([Path(tmp_path)])

    assert "ios-only" in skills.list(1024, channel="ios")
    assert "ios-only" not in skills.list(1024, channel="web")
    assert skills.find("application", channel="ios")
    assert not skills.find("application", channel="web")
    assert skills.load("ios-only", channel="ios") == "Only on iOS."
