"""Task-bound teammate and leaf delegation owned by an active TaskAgent."""

import json
import time

from core.tasks.entities import AgentRole, binding, execution
from core.tasks.modes import execution_mode


def scoped_call(db, uid, operation, p):
    from core.subagents import storage

    actor = binding(db, uid)
    run = execution(db, uid)
    if not actor or actor.role != AgentRole.TASK_AGENT:
        raise ValueError("Only a TaskAgent may delegate; leaf subagents cannot delegate")
    task = db.execute("SELECT * FROM tasks WHERE task_id=?", (actor.task_id,)).fetchone()
    if run["status"] != "running" or task["lifecycle"] != "active":
        raise ValueError("Task execution is not active")
    mode = execution_mode(json.loads(run["spec_snapshot_json"])["executor"])
    if operation == "subagent.spawn":
        if mode == "dynamic_workflow":
            raise ValueError("Dynamic workflows create Subagents through workflow_define")
        params = {**p, "request_id": "taskleaf:" + actor.agent_id + ":" + p["request_id"]}
        child = storage.spawn(db, actor.user_id, params)
        inserted = db.execute(
            (
                "INSERT OR IGNORE INTO "
                "task_delegations(user_id,task_id,run_id,parent_agent_id,agen"
                "t_id,created_at,role) VALUES(?,?,?,?,?,?,?)"
            ),
            (
                actor.user_id,
                actor.task_id,
                actor.run_id,
                actor.agent_id,
                child["agent_id"],
                time.time(),
                "teammate" if mode == "agent_team" else "subagent",
            ),
        ).rowcount
        if not inserted:
            return child
        from core.tasks.storage import event

        event(
            db,
            task,
            "subagent.created",
            {
                "agent_id": child["agent_id"],
                "parent_agent_id": actor.agent_id,
                "title": child["title"],
            },
            run,
        )
        return child
    if operation == "subagent.list":
        return [
            storage.get(db, actor.user_id, row[0])
            for row in db.execute(
                "SELECT agent_id FROM task_delegations WHERE parent_agent_id=? ORDER BY created_at",
                (actor.agent_id,),
            )
        ]
    allowed = db.execute(
        "SELECT 1 FROM task_delegations WHERE parent_agent_id=? AND agent_id=?",
        (actor.agent_id, p.get("agent_id")),
    ).fetchone()
    if not allowed:
        raise ValueError("Subagent is not owned by this TaskAgent")
    return storage.call(db, actor.user_id, operation, {**p, "user_id": actor.user_id})


def notify_parent(db, row, type_, payload, key):
    delegation = db.execute(
        "SELECT * FROM task_delegations WHERE agent_id=?", (row["agent_id"],)
    ).fetchone()
    if not delegation:
        return False
    parent = db.execute(
        "SELECT storage_id FROM subagents WHERE agent_id=?", (delegation["parent_agent_id"],)
    ).fetchone()
    from core.tasks.inbox import deliver
    from core.tasks.storage import event, owned

    deliver(
        db,
        parent["storage_id"],
        type_,
        {"agent_id": row["agent_id"], "title": row["title"], **payload},
        key,
    )
    run = db.execute("SELECT * FROM task_runs WHERE run_id=?", (delegation["run_id"],)).fetchone()
    event(
        db,
        owned(db, delegation["user_id"], delegation["task_id"]),
        type_,
        {"agent_id": row["agent_id"], "parent_agent_id": delegation["parent_agent_id"], **payload},
        run,
    )
    return True


def control_children(db, run_id, action):
    from core.subagents.storage import finish, resume

    rows = db.execute(
        (
            "SELECT a.*,d.state AS delegation_state,d.role AS role FROM subagents a "
            "JOIN task_delegations d USING(agent_id) WHERE d.run_id=?"
        ),
        (run_id,),
    ).fetchall()
    for child in rows:
        if action == "cancel" and child["delegation_state"] == "paused":
            db.execute(
                "UPDATE task_delegations SET state='closed' WHERE agent_id=?", (child["agent_id"],)
            )
            db.execute(
                "UPDATE subagents SET status='cancelled',result=?,finished_at=? WHERE agent_id=?",
                (
                    "Owning task cancelled",
                    time.time(),
                    child["agent_id"],
                ),
            )
            continue
        if action == "resume" and child["delegation_state"] == "paused":
            db.execute(
                "UPDATE task_delegations SET state='active' WHERE agent_id=?", (child["agent_id"],)
            )
            # Workflow nodes are resumed only when the dependency scheduler says
            # they are ready; resuming every paused node here would break the DAG.
            if child["role"] != "workflow_node" and child["status"] != "running":
                resume(
                    db,
                    child["user_id"],
                    {
                        "agent_id": child["agent_id"],
                        "message_id": "resume:" + str(time.time_ns()),
                        "content": "Resume your bounded assignment from saved progress.",
                    },
                )
        elif action in {"pause", "cancel"} and child["status"] == "running":
            db.execute(
                "UPDATE task_delegations SET state=? WHERE agent_id=?",
                ("paused" if action == "pause" else "closed", child["agent_id"]),
            )
            finish(
                db, child, "failed" if action == "pause" else "cancelled", "Owning task " + action
            )
        elif action in {"pause", "cancel"} and child["status"] == "waiting":
            db.execute(
                "UPDATE task_delegations SET state=? WHERE agent_id=?",
                ("paused" if action == "pause" else "closed", child["agent_id"]),
            )
            if action == "cancel":
                db.execute(
                    "UPDATE subagents SET status='cancelled',result=?,finished_at=? "
                    "WHERE agent_id=?",
                    ("Owning task cancelled", time.time(), child["agent_id"]),
                )
        elif action in {"pause", "cancel"} and child["status"] == "paused":
            db.execute(
                "UPDATE task_delegations SET state=? WHERE agent_id=?",
                ("paused" if action == "pause" else "closed", child["agent_id"]),
            )
            if action == "cancel":
                db.execute(
                    "UPDATE subagents SET status='cancelled',result=?,finished_at=? "
                    "WHERE agent_id=?",
                    ("Owning task cancelled", time.time(), child["agent_id"]),
                )
        if action == "pause" and child["role"] == "workflow_node":
            db.execute(
                "UPDATE task_workflow_nodes SET status='pending',result_json=NULL,"
                "error_json=NULL,started_at=NULL,finished_at=NULL "
                "WHERE agent_id=? AND status!='succeeded'",
                (child["agent_id"],),
            )
        elif action == "cancel" and child["role"] == "workflow_node":
            db.execute(
                "UPDATE task_workflow_nodes SET status='cancelled',finished_at=? "
                "WHERE agent_id=? AND status NOT IN ('succeeded','cancelled')",
                (time.time(), child["agent_id"]),
            )
