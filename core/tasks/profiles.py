"""Task tool profiles; default still passes through existing permission checks."""

READ_ONLY_TOOLS = frozenset(
    {"task_report", "task_fetch", "skill", "detail_read", "recall", "context_memory"}
)


def allowed(profile, name):
    if name == "subagent_notify":
        return False
    return profile == "default" or name in READ_ONLY_TOOLS
