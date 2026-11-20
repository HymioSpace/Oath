from core.subagents.tools import SubagentTool
from core.tasks.collaboration_tools import CollaborationTool
from core.tasks.prompts import INTERACTION, LEAF, TASK_AGENT
from core.tasks.tools import TaskTool


class Storage:
    def call(self, *_args, **_kwargs):
        return {}


def test_role_prompts_require_explicit_task_boundaries():
    for prompt in (INTERACTION, TASK_AGENT, LEAF):
        assert "bound" in prompt.lower() or "boundary" in prompt.lower()
    assert "non-goals" in INTERACTION
    assert "in-scope work" in TASK_AGENT
    assert "hard boundary" in LEAF
    assert "every eight active steps" in TASK_AGENT
    assert "temporarily unavailable" in TASK_AGENT
    assert "Never cancel" in TASK_AGENT


def test_delegation_tool_descriptions_require_inputs_scope_and_output():
    spawn = SubagentTool(Storage(), "spawn").definition.description
    workflow = CollaborationTool(Storage(), "workflow_define").definition.description
    for text in (spawn, workflow):
        assert "in-scope" in text
        assert "deliverable" in text
    assert "non-goal" in spawn
    assert "verifiable success criteria" in workflow


def test_task_creation_schema_requires_an_explicit_boundary():
    definition = TaskTool(Storage(), "create").definition
    objective = definition.input_schema["properties"]["objective"]["description"]
    assert "one coherent" in definition.description
    assert "in-scope work" in objective
    assert "exclusions or non-goals" in objective
    assert "stop/blocker condition" in objective
