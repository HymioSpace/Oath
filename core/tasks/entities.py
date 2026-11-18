"""Explicit execution roles. Identity stays account-scoped; delegation never nests IDs."""

from dataclasses import dataclass
from enum import StrEnum


class AgentRole(StrEnum):
    INTERACTION = "interaction"
    TASK_AGENT = "task_agent"
    SUBAGENT = "subagent"


@dataclass(frozen=True)
class ExecutionBinding:
    role: AgentRole
    user_id: str
    task_id: str
    run_id: str
    agent_id: str
    parent_agent_id: str | None = None


def binding(db, context_id):
    row = db.execute(
        "SELECT user_id,task_id,run_id,agent_id FROM task_runs WHERE context_id=?", (context_id,)
    ).fetchone()
    if row:
        return ExecutionBinding(AgentRole.TASK_AGENT, **dict(row))
    row = db.execute(
        (
            "SELECT d.user_id,d.task_id,d.run_id,d.agent_id,d.parent_agen"
            "t_id FROM task_delegations d JOIN subagents a ON "
            "a.agent_id=d.agent_id WHERE a.storage_id=?"
        ),
        (context_id,),
    ).fetchone()
    return ExecutionBinding(AgentRole.SUBAGENT, **dict(row)) if row else None


def execution(db, context_id):
    actor = binding(db, context_id)
    return (
        db.execute("SELECT * FROM task_runs WHERE run_id=?", (actor.run_id,)).fetchone()
        if actor
        else None
    )
