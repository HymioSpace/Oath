"""Dense, owner-scoped execution graph for Task panels and agent queries."""

import json


def inspect_execution(db, task, run):
    from core.subagents.storage import activity, get

    events = [
        {
            "id": r["event_id"],
            "type": r["type"],
            "created_at": r["created_at"],
            "data": json.loads(r["payload_json"]),
        }
        for r in db.execute(
            (
                "SELECT * FROM task_events WHERE user_id=? AND task_id=? "
                "ORDER BY event_id DESC LIMIT 50"
            ),
            (task["user_id"], task["task_id"]),
        )
    ]
    if not run:
        return {
            "executor": json.loads(task["executor_json"])["kind"],
            "task_agent": None,
            "subagents": [],
            "events": events,
            "plan": json.loads(task["plan_json"]),
        }
    actor = None
    if run["agent_id"]:
        actor = get(db, task["user_id"], run["agent_id"])
        snapshot = json.loads(run["spec_snapshot_json"])
        actor.update(
            role="task_agent", execution_status=run["status"], objective=snapshot["objective"]
        )
        actor["activity"] = activity(
            db, task["user_id"], {"agent_id": run["agent_id"], "limit": 40}
        )
    children = []
    for row in db.execute(
        "SELECT agent_id,parent_agent_id FROM task_delegations WHERE run_id=? ORDER BY created_at",
        (run["run_id"],),
    ):
        child = get(db, task["user_id"], row["agent_id"])
        child.update(
            role="subagent",
            parent_agent_id=row["parent_agent_id"],
            execution_status=(
                run["status"]
                if run["status"] in {"paused", "cancelled"} and child["status"] == "running"
                else child["status"]
            ),
        )
        child["activity"] = activity(
            db, task["user_id"], {"agent_id": row["agent_id"], "limit": 40}
        )
        children.append(child)
    return {
        "executor": json.loads(run["spec_snapshot_json"])["executor"]["kind"],
        "task_agent": actor,
        "subagents": children,
        "events": events,
        "checkpoint": json.loads(run["progress_json"]),
        "wait": json.loads(run["wait_json"]) if run["wait_json"] else None,
        "usage": json.loads(run["usage_json"]),
        "plan": json.loads(task["plan_json"]),
    }
