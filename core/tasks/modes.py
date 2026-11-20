"""Canonical execution modes shared by task validation, tools and inspection."""

EXECUTION_MODES = (
    "program",
    "parallel_subagents",
    "agent_team",
    "dynamic_workflow",
)

DEFAULT_AGENT_MODE = "parallel_subagents"


def execution_mode(executor):
    if not isinstance(executor, dict):
        raise ValueError("Executor must be an object")
    if executor.get("kind") == "program":
        return "program"
    if executor.get("kind") != "agent":
        raise ValueError("Unknown executor kind")
    mode = executor.get("mode") or DEFAULT_AGENT_MODE
    if mode not in EXECUTION_MODES[1:]:
        raise ValueError("Unknown agent execution mode")
    return mode


def normalize_executor(executor):
    value = dict(executor)
    if value.get("kind") == "agent":
        value["mode"] = execution_mode(value)
    return value
