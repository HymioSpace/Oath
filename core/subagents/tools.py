"""Small tools over durable delegation operations."""

from core.protocol.datatype.tool import ToolDefinition, ToolResult
from core.protocol.error import ToolExecutionError
from core.protocol.interface.tool import Tool

_FIELDS = {
    "agent_id": {"type": "string"},
    "title": {"type": "string"},
    "objective": {
        "type": "string",
        "description": (
            "One bounded deliverable. State source inputs, in-scope work, "
            "excluded/non-goal work, and the exact result to return."
        ),
    },
    "success_criteria": {
        "type": "string",
        "description": "Observable checks that prove the bounded deliverable is complete.",
    },
    "content": {
        "type": "string",
        "description": (
            "Bounded continuation instructions. Include any changed inputs, scope, "
            "exclusions or deliverable so the existing subagent boundary stays explicit."
        ),
    },
    "model": {"type": "string"},
    "max_steps": {"type": "integer", "minimum": 1, "maximum": 100},
    "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 3600},
}
_SPECS = {
    "spawn": (
        "Delegate one bounded objective asynchronously. The objective must name the concrete "
        "deliverable, in-scope work, excluded/non-goal work and required source inputs. "
        "Returns agent_id immediately. "
        "Completion arrives as subagent.completed; do not poll in a loop.",
        ["title", "objective", "success_criteria"],
        ["title", "objective", "success_criteria", "model", "max_steps", "timeout_seconds"],
    ),
    "list": ("List recent delegated executions and their status.", [], []),
    "get": (
        "Read one delegated execution, intermediate findings and final result.",
        ["agent_id"],
        ["agent_id"],
    ),
    "cancel": (
        "Cancel a delegated execution; already dispatched external actions cannot be undone.",
        ["agent_id"],
        ["agent_id"],
    ),
    "continue": (
        "Send new bounded instructions to an owned subagent and continue from its saved context. "
        "Restate any changed scope or acceptance boundary.",
        ["agent_id", "content"],
        ["agent_id", "content", "success_criteria", "max_steps", "timeout_seconds"],
    ),
    "notify": (
        "Send a significant intermediate finding to the parent while continuing your own work.",
        ["content"],
        ["content"],
    ),
}


class SubagentTool(Tool):
    def __init__(self, storage, action):
        self.storage, self.action = storage, action
        self.allow_subagent = True
        # Subagents are execution details of a TaskAgent, never ad-hoc children of the
        # interaction agent. Temporary parallel work must first become a Task.
        self.subagent_only = True
        description, required, fields = _SPECS[action]
        self._definition = ToolDefinition(
            "subagent_" + action,
            description,
            {
                "type": "object",
                "properties": {k: _FIELDS[k] for k in fields},
                "required": required,
                "additionalProperties": False,
            },
        )

    @property
    def definition(self):
        return self._definition

    def execute(self, invocation):
        try:
            parameters = dict(invocation.arguments)
            if self.action == "spawn":
                parameters["request_id"] = invocation.invocation_id
            if self.action == "notify":
                parameters["note_id"] = invocation.invocation_id
            if self.action == "continue":
                parameters["message_id"] = invocation.invocation_id
            return ToolResult(
                self.storage.call(
                    "subagent." + self.action, user_id=invocation.user.storage_id, **parameters
                )
            )
        except ValueError as exc:
            raise ToolExecutionError(str(exc)) from exc


def tools(storage):
    return tuple(SubagentTool(storage, action) for action in _SPECS)
