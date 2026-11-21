from pathlib import Path

import pytest
from jsonschema import ValidationError, validate

from core.protocol.datatype import ToolDefinition, ToolResult
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

    ios = registry.definitions(channel="ios")
    web = registry.definitions(channel="web")
    every = registry.definitions()
    assert [definition.name for definition in ios] == ["open", "scoped"]
    assert [definition.name for definition in web] == ["open"]
    assert [definition.name for definition in every] == ["open", "scoped"]
    for definition in every:
        assert definition.input_schema["properties"]["description"]["minLength"] == 1
        assert "description" in definition.input_schema["required"]
        with pytest.raises(ValidationError):
            validate({}, definition.input_schema)


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
