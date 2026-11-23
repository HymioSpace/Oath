from core.subagents.tools import SubagentTool
from core.tasks.collaboration_tools import CollaborationTool
from core.tasks.prompts import (
    INTERACTION,
    LEAF,
    TASK_AGENT,
    leaf_prompt,
    task_agent_prompt,
)
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


def test_selected_execution_mode_is_injected_as_a_hard_task_contract():
    parallel = task_agent_prompt(
        {
            "execution_mode": "parallel_subagents",
            "occurrence": {"trigger_mode": "now", "completion": "achieve"},
        }
    )
    team = task_agent_prompt(
        {
            "execution_mode": "agent_team",
            "occurrence": {"trigger_mode": "now", "completion": "achieve"},
        }
    )
    workflow = task_agent_prompt(
        {
            "execution_mode": "dynamic_workflow",
            "occurrence": {"trigger_mode": "now", "completion": "achieve"},
        }
    )
    assert "parallel_subagents" in parallel
    assert "at least two" in parallel
    assert "leaf Agents with subagent_spawn" in parallel
    assert "report blocked" in parallel
    assert "at least two bounded teammates" in team
    assert "team_send" in team
    assert "workflow_define" in workflow
    assert "dependency edges" in workflow
    assert "dynamic_workflow" in leaf_prompt({"execution_mode": "dynamic_workflow"})


def test_recurring_task_prompt_is_scoped_to_one_occurrence():
    prompt = task_agent_prompt(
        {
            "execution_mode": "parallel_subagents",
            "occurrence": {
                "due_at": "2026-11-21T12:00:00+08:00",
                "trigger_mode": "daily",
                "completion": "maintain",
            },
        }
    )
    assert "exactly one occurrence" in prompt
    assert "2026-11-21T12:00:00+08:00" in prompt
    assert "do not merge multiple due times" in prompt
    assert "Previous task notes are bounded history" in prompt


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
