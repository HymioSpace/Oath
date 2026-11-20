"""Model-facing Agent Team and dynamic workflow operations."""

from core.protocol.datatype.tool import ToolDefinition, ToolResult
from core.protocol.error import ToolExecutionError
from core.protocol.interface.tool import Tool


class CollaborationTool(Tool):
    subagent_only = True
    allow_subagent = True

    def __init__(self, storage, action):
        self.storage = storage
        self.action = action

    @property
    def definition(self):
        if self.action == "team_send":
            return ToolDefinition(
                "team_send",
                "Send a durable message to one teammate, or broadcast to the Agent Team "
                "when recipient_agent_id is omitted. Use team_members to discover peers.",
                {
                    "type": "object",
                    "properties": {
                        "recipient_agent_id": {
                            "type": "string",
                            "description": (
                                "A leader or teammate in the same TaskRun; "
                                "omit to broadcast."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 4000,
                        },
                    },
                    "required": ["content"],
                    "additionalProperties": False,
                },
            )
        if self.action == "team_inbox":
            return ToolDefinition(
                "team_inbox",
                "Read durable messages sent by or to this Agent Team member.",
                {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                    "additionalProperties": False,
                },
            )
        if self.action == "team_members":
            return ToolDefinition(
                "team_members",
                "List the leader and teammates bound to this TaskRun with their current status.",
                {"type": "object", "properties": {}, "additionalProperties": False},
            )
        if self.action == "workflow_define":
            return ToolDefinition(
                "workflow_define",
                "Define the dynamic workflow DAG once. Nodes are bound Subagents and must each "
                "state one concrete deliverable, in-scope work, exclusions/non-goals and "
                "verifiable success criteria in objective/success_criteria. A node starts only "
                "after all dependencies succeed. The TaskAgent is woken after the join.",
                {
                    "type": "object",
                    "properties": {
                        "nodes": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 20,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {
                                        "type": "string",
                                        "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,63}$",
                                    },
                                    "title": {"type": "string", "minLength": 1, "maxLength": 160},
                                    "objective": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 8000,
                                        "description": (
                                            "One bounded deliverable with source inputs, "
                                            "in-scope work, exclusions and non-goals."
                                        ),
                                    },
                                    "success_criteria": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 8000,
                                        "description": (
                                            "Observable checks that prove this node's "
                                            "bounded output is complete."
                                        ),
                                    },
                                    "depends_on": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "maxItems": 20,
                                        "uniqueItems": True,
                                    },
                                },
                                "required": [
                                    "id",
                                    "title",
                                    "objective",
                                    "success_criteria",
                                ],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["nodes"],
                    "additionalProperties": False,
                },
            )
        return ToolDefinition(
            "workflow_status",
            "Read workflow nodes, dependency status and bounded node results.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        )

    def execute(self, invocation):
        arguments = dict(invocation.arguments)
        if self.action == "workflow_define":
            arguments["request_id"] = invocation.invocation_id
        if self.action == "team_send":
            arguments["request_id"] = invocation.invocation_id
        try:
            if self.action.startswith("team_"):
                operation = "team." + self.action.removeprefix("team_")
            elif self.action == "workflow_define":
                operation = "workflow.define"
            else:
                operation = "workflow.get"
            return ToolResult(
                self.storage.call(operation, user_id=invocation.user.storage_id, **arguments)
            )
        except ValueError as exc:
            raise ToolExecutionError(str(exc)) from exc


def tools(storage):
    return tuple(
        CollaborationTool(storage, action)
        for action in (
            "team_send",
            "team_inbox",
            "team_members",
            "workflow_define",
            "workflow_status",
        )
    )
