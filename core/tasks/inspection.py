"""Dense, owner-scoped execution graph for Task panels and agent queries."""

import json

from core.tasks.modes import execution_mode
from core.tasks.workflow import list_nodes


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
        executor = json.loads(task["executor_json"])
        return {
            "executor": executor["kind"],
            "mode": execution_mode(executor),
            "task_agent": None,
            "subagents": [],
            "events": events,
            "plan": json.loads(task["plan_json"]),
            "team_messages": [],
            "workflow": {"nodes": [], "edges": []},
        }
    snapshot = json.loads(run["spec_snapshot_json"])
    mode = execution_mode(snapshot["executor"])
    messages = [
        {
            "id": row["message_id"],
            "sender_agent_id": row["sender_agent_id"],
            "recipient_agent_id": row["recipient_agent_id"],
            "sender_title": row["sender_title"],
            "recipient_title": row["recipient_title"],
            "content": row["content"],
            "created_at": row["created_at"],
        }
        for row in db.execute(
            "SELECT m.message_id,m.sender_agent_id,m.recipient_agent_id,m.content,m.created_at,"
            "s.title AS sender_title,r.title AS recipient_title "
            "FROM task_team_messages m "
            "JOIN subagents s ON s.agent_id=m.sender_agent_id "
            "JOIN subagents r ON r.agent_id=m.recipient_agent_id "
            "WHERE m.run_id=? ORDER BY m.created_at,m.message_id",
            (run["run_id"],),
        )
    ]

    def communications(agent_id):
        if agent_id == run["agent_id"]:
            return messages
        return [
            item
            for item in messages
            if agent_id in {item["sender_agent_id"], item["recipient_agent_id"]}
        ]

    actor = None
    if run["agent_id"]:
        actor = get(db, task["user_id"], run["agent_id"])
        actor.update(
            role="task_agent", execution_status=run["status"], objective=snapshot["objective"]
        )
        if run["result_json"]:
            actor["result"] = json.loads(run["result_json"])["summary"]
        actor["activity"] = activity(
            db, task["user_id"], {"agent_id": run["agent_id"], "limit": 200}
        )
        actor["communications"] = communications(run["agent_id"])
    children = []
    for row in db.execute(
        "SELECT agent_id,parent_agent_id,role,workflow_node_id "
        "FROM task_delegations WHERE run_id=? ORDER BY created_at",
        (run["run_id"],),
    ):
        child = get(db, task["user_id"], row["agent_id"])
        child.update(
            role="subagent",
            parent_agent_id=row["parent_agent_id"],
            delegation_role=row["role"],
            workflow_node_id=row["workflow_node_id"],
            execution_status=(
                run["status"]
                if run["status"] in {"paused", "cancelled"} and child["status"] == "running"
                else child["status"]
            ),
        )
        child["activity"] = activity(
            db, task["user_id"], {"agent_id": row["agent_id"], "limit": 200}
        )
        child["communications"] = communications(row["agent_id"])
        children.append(child)
    nodes = list_nodes(db, run["run_id"])
    edges = [
        {"from": dependency, "to": node["id"]}
        for node in nodes
        for dependency in node["depends_on"]
    ]
    return {
        "executor": snapshot["executor"]["kind"],
        "mode": mode,
        "task_agent": actor,
        "subagents": children,
        "events": events,
        "checkpoint": json.loads(run["progress_json"]),
        "wait": json.loads(run["wait_json"]) if run["wait_json"] else None,
        "usage": json.loads(run["usage_json"]),
        "plan": json.loads(task["plan_json"]),
        "team_messages": messages,
        "workflow": {"nodes": nodes, "edges": edges},
    }
