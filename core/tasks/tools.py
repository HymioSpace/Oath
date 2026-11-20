"""Task tools use the same schemas at the model and persistence boundary."""

from core.protocol.datatype.tool import ToolDefinition, ToolResult
from core.protocol.error import ToolExecutionError
from core.protocol.interface.tool import Tool
from core.tasks.schema import tool_schema
from core.tasks.storage import SPECS

DESCRIPTIONS = {
    "create": (
        "Create accepted background work with goal, context, "
        "trigger, executor and budget. Reuse intent_key; fixed "
        "reminders use program notify and must not allocate a TaskAgent or subagent. "
        "Agent work defaults to parallel_subagents. Use agent_team only when teammates "
        "must communicate during the work. Use dynamic_workflow when the main Agent must "
        "define dependencies between bounded Agent nodes before execution. "
        "Returns immediately."
    ),
    "list": (
        "List relevant durable tasks and attention requests. Query "
        "before creating duplicate work. Do not poll for completion."
    ),
    "get": (
        "Read a task, its requirements, current execution or "
        "history. Use this to answer progress questions or verify a "
        "result."
    ),
    "update": (
        "Change task requirements with expected_revision. Use future "
        "for upcoming runs; pause and settle active work before "
        "current_and_future."
    ),
    "message": (
        "Add context or answer a task question using its reply_to "
        "question ID. Does not change the goal or resume a "
        "user-paused task."
    ),
    "pause": (
        "Pause the task, future triggers and owned subtasks. Already "
        "executed actions remain recorded."
    ),
    "resume": (
        "Resume a paused task after checking its blocker and "
        "remaining budget. Use the latest revision."
    ),
    "cancel": (
        "Close the task and stop future actions and owned subtasks. "
        "Cancellation cannot undo prior external actions."
    ),
    "retry": (
        "Retry a failed execution only after checking known and "
        "unknown effects. Preserve the original run and accumulated "
        "budget."
    ),
    "report": (
        "Report meaningful progress, continuation, a concrete wait, "
        "completion evidence or a blocker for your own task. "
        "Complete when your goal is achieved; report a result and its supporting references. "
        "Use an explicit user wait when a decision is required. Notification delivery is separate."
    ),
}


class TaskTool(Tool):
    def __init__(self, storage, action):
        (self.storage, self.action) = (storage, action)
        self.allow_subagent = action == "report"
        self.subagent_only = action == "report"

    @property
    def definition(self):
        return ToolDefinition(
            "task_" + self.action,
            DESCRIPTIONS[self.action],
            tool_schema(SPECS[self.action]),
        )

    def execute(self, invocation):
        try:
            return ToolResult(
                self.storage.call(
                    "task." + self.action,
                    user_id=invocation.user.storage_id,
                    request_id=invocation.invocation_id,
                    **invocation.arguments,
                )
            )
        except ValueError as exc:
            raise ToolExecutionError(str(exc)) from exc


def tools(storage):
    return tuple(TaskTool(storage, action) for action in SPECS)
