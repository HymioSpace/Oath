"""Durable DAG nodes for tasks whose TaskAgent defines a dynamic workflow."""

import json
import re
import time
import uuid

from core.storage.database import encode
from core.tasks.entities import AgentRole, binding, execution
from core.tasks.modes import execution_mode

NODE_STATUSES = {"pending", "running", "paused", "succeeded", "failed", "cancelled", "blocked"}
TERMINAL = {"succeeded", "failed", "cancelled", "blocked"}
MAX_NODES = 20
MAX_ACTIVE = 8


def _task_context(db, uid):
    actor = binding(db, uid)
    run = execution(db, uid)
    if not actor or actor.role != AgentRole.TASK_AGENT or not run:
        raise ValueError("Only a TaskAgent may define a workflow")
    task = db.execute(
        "SELECT * FROM tasks WHERE user_id=? AND task_id=?", (actor.user_id, actor.task_id)
    ).fetchone()
    snapshot = json.loads(run["spec_snapshot_json"])
    if execution_mode(snapshot["executor"]) != "dynamic_workflow":
        raise ValueError("workflow_define requires dynamic_workflow execution mode")
    if run["status"] not in {"running", "waiting"} or task["lifecycle"] != "active":
        raise ValueError("Task execution is not active")
    return actor, run, task


def define(db, uid, p, request_id):
    actor, run, task = _task_context(db, uid)
    nodes = p.get("nodes")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES:
        raise ValueError(f"Workflow requires 1..{MAX_NODES} nodes")
    normalized = _normalize_nodes(nodes)
    existing = list_nodes(db, run["run_id"])
    if existing:
        if _node_shape(existing) != normalized:
            raise ValueError("Workflow already defined with different nodes")
        return {"nodes": existing, "status": workflow_status(db, run["run_id"])}
    if run["status"] != "running":
        raise ValueError("Task execution is not active")
    now = time.time()
    from core.subagents.storage import spawn

    snapshot = json.loads(run["spec_snapshot_json"])
    budget = snapshot["policy"]["run_budget"]
    for position, node in enumerate(normalized):
        child = spawn(
            db,
            actor.user_id,
            {
                "request_id": f"wf:{run['run_id']}:{node['id']}",
                "title": node["title"],
                "objective": node["objective"],
                "success_criteria": node["success_criteria"],
                "max_steps": max(1, min(100, int(budget["max_steps"]))),
                "timeout_seconds": max(10, min(3600, int(budget["max_active_seconds"] or 900))),
                "defer_start": True,
                "notify_parent": True,
            },
        )
        db.execute(
            "INSERT INTO task_delegations(user_id,task_id,run_id,parent_agent_id,"
            "agent_id,state,created_at,role,workflow_node_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                actor.user_id,
                actor.task_id,
                actor.run_id,
                actor.agent_id,
                child["agent_id"],
                "active",
                now,
                "workflow_node",
                node["id"],
            ),
        )
        db.execute(
            "INSERT INTO task_workflow_nodes(run_id,user_id,task_id,node_id,position,"
            "title,objective,success_criteria,depends_on_json,agent_id,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                actor.run_id,
                actor.user_id,
                actor.task_id,
                node["id"],
                position,
                node["title"],
                node["objective"],
                node["success_criteria"],
                encode(node["depends_on"]),
                child["agent_id"],
                "pending",
                now,
            ),
        )
    wait = {
        "wait_id": uuid.uuid4().hex,
        "condition": {
            "kind": "workflow",
            "reason": "Executing workflow dependencies",
        },
        "signal_floor": run["signal_floor"],
        "next_check_at": None,
    }
    db.execute(
        "UPDATE task_runs SET status='waiting',wait_json=?,next_check_at=NULL,"
        "version=version+1 WHERE run_id=?",
        (encode(wait), run["run_id"]),
    )
    from core.tasks.storage import event

    event(
        db,
        task,
        "workflow_defined",
        {
            "request_id": request_id,
            "nodes": [{"id": item["id"], "depends_on": item["depends_on"]} for item in normalized],
        },
        db.execute("SELECT * FROM task_runs WHERE run_id=?", (run["run_id"],)).fetchone(),
    )
    advance(db, task, run, wake=False)
    return {"nodes": list_nodes(db, run["run_id"]), "status": workflow_status(db, run["run_id"])}


def _normalize_nodes(nodes):
    result = []
    ids = set()
    for value in nodes:
        if not isinstance(value, dict):
            raise ValueError("Workflow nodes must be objects")
        node_id = value.get("id")
        title = value.get("title")
        objective = value.get("objective")
        criteria = value.get("success_criteria")
        depends_on = value.get("depends_on", [])
        if not isinstance(node_id, str) or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_-]{0,63}", node_id
        ):
            raise ValueError("Workflow node id is invalid")
        if node_id in ids:
            raise ValueError("Workflow node ids must be unique")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 160:
            raise ValueError("Workflow node title is required")
        if not isinstance(objective, str) or not 1 <= len(objective.strip()) <= 8000:
            raise ValueError("Workflow node objective is required")
        if not isinstance(criteria, str) or not 1 <= len(criteria.strip()) <= 8000:
            raise ValueError("Workflow node success criteria is required")
        if (
            not isinstance(depends_on, list)
            or len(depends_on) > MAX_NODES
            or any(not isinstance(item, str) for item in depends_on)
            or len(set(depends_on)) != len(depends_on)
        ):
            raise ValueError("Workflow dependencies must be unique node ids")
        ids.add(node_id)
        result.append(
            {
                "id": node_id,
                "title": title.strip(),
                "objective": objective.strip(),
                "success_criteria": criteria.strip(),
                "depends_on": list(depends_on),
            }
        )
    known = {item["id"] for item in result}
    for node in result:
        if any(item not in known or item == node["id"] for item in node["depends_on"]):
            raise ValueError("Workflow contains an unknown or self dependency")
    _topological(result)
    return result


def _topological(nodes):
    dependencies = {item["id"]: set(item["depends_on"]) for item in nodes}
    ready = [item["id"] for item in nodes if not dependencies[item["id"]]]
    visited = []
    while ready:
        node = ready.pop(0)
        visited.append(node)
        for candidate in nodes:
            key = candidate["id"]
            if node in dependencies[key]:
                dependencies[key].remove(node)
                if not dependencies[key]:
                    ready.append(key)
    if len(visited) != len(nodes):
        raise ValueError("Workflow dependencies must form a DAG without cycles")


def _node_shape(nodes):
    return [
        {
            "id": item["id"],
            "title": item["title"],
            "objective": item["objective"],
            "success_criteria": item["success_criteria"],
            "depends_on": item["depends_on"],
        }
        for item in nodes
    ]


def list_nodes(db, run_id):
    return [
        {
            "id": row["node_id"],
            "title": row["title"],
            "objective": row["objective"],
            "success_criteria": row["success_criteria"],
            "depends_on": json.loads(row["depends_on_json"]),
            "agent_id": row["agent_id"],
            "status": row["status"],
            "result": json.loads(row["result_json"])["summary"] if row["result_json"] else "",
            "error": json.loads(row["error_json"]).get("message", "") if row["error_json"] else "",
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }
        for row in db.execute(
            "SELECT * FROM task_workflow_nodes WHERE run_id=? ORDER BY position",
            (run_id,),
        )
    ]


def workflow_status(db, run_id):
    rows = db.execute(
        "SELECT status,COUNT(*) AS n FROM task_workflow_nodes WHERE run_id=? GROUP BY status",
        (run_id,),
    ).fetchall()
    counts = {row["status"]: row["n"] for row in rows}
    if counts.get("failed") or counts.get("blocked") or counts.get("cancelled"):
        state = "failed"
    elif counts.get("paused"):
        state = "paused"
    elif counts.get("running"):
        state = "running"
    elif counts and counts.get("succeeded") == sum(counts.values()):
        state = "succeeded"
    else:
        state = "waiting"
    return {"state": state, "counts": counts}


def has_nodes(db, run_id):
    return bool(
        db.execute("SELECT 1 FROM task_workflow_nodes WHERE run_id=? LIMIT 1", (run_id,)).fetchone()
    )


def _dependency_outputs(db, run_id, dependency_ids):
    outputs = []
    total_chars = 0
    for dependency_id in dependency_ids:
        row = db.execute(
            "SELECT title,result_json FROM task_workflow_nodes WHERE run_id=? AND node_id=?",
            (run_id, dependency_id),
        ).fetchone()
        if not row or not row["result_json"]:
            raise ValueError(f"Dependency result is missing for node {dependency_id}")
        result = json.loads(row["result_json"])
        summary = result.get("summary") if isinstance(result, dict) else ""
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError(f"Dependency result is empty for node {dependency_id}")
        available = max(0, 24000 - total_chars)
        if available == 0:
            break
        bounded = summary.strip()[: min(8000, available)]
        outputs.append(f"## {dependency_id}: {row['title']}\n{bounded}")
        total_chars += len(bounded)
    return "\n\n".join(outputs) or "No dependency outputs."


def advance(db, task, run, wake=True):
    """Reconcile child outcomes, release ready nodes and wake the leader at the join."""
    nodes = db.execute(
        "SELECT * FROM task_workflow_nodes WHERE run_id=? ORDER BY position", (run["run_id"],)
    ).fetchall()
    if not nodes:
        return False
    now = time.time()
    changed = False
    for node in nodes:
        child = db.execute(
            "SELECT * FROM subagents WHERE agent_id=?", (node["agent_id"],)
        ).fetchone()
        if not child:
            continue
        if node["status"] in {"running", "paused"} and child["status"] == "paused":
            db.execute(
                "UPDATE task_workflow_nodes SET status='paused',result_json=NULL,"
                "error_json=NULL,finished_at=NULL WHERE run_id=? AND node_id=?",
                (run["run_id"], node["node_id"]),
            )
            changed = True
        elif node["status"] in {"pending", "running"} and child["status"] in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            status = child["status"]
            result = {"summary": child["result"]} if status == "succeeded" else None
            error = (
                {"code": "NODE_FAILED", "message": child["result"]}
                if status != "succeeded"
                else None
            )
            db.execute(
                "UPDATE task_workflow_nodes SET status=?,result_json=?,error_json=?,"
                "finished_at=? WHERE run_id=? AND node_id=?",
                (
                    status,
                    encode(result) if result else None,
                    encode(error) if error else None,
                    now,
                    run["run_id"],
                    node["node_id"],
                ),
            )
            changed = True
    nodes = db.execute(
        "SELECT * FROM task_workflow_nodes WHERE run_id=? ORDER BY position", (run["run_id"],)
    ).fetchall()
    states = {node["node_id"]: node["status"] for node in nodes}
    for node in nodes:
        if node["status"] not in {"pending", "paused"}:
            continue
        deps = json.loads(node["depends_on_json"])
        if any(states[item] in {"failed", "cancelled", "blocked"} for item in deps):
            db.execute(
                "UPDATE task_workflow_nodes SET status='blocked',error_json=?,finished_at=? "
                "WHERE run_id=? AND node_id=?",
                (
                    encode(
                        {
                            "code": "DEPENDENCY_FAILED",
                            "message": "A dependency did not complete successfully",
                        }
                    ),
                    now,
                    run["run_id"],
                    node["node_id"],
                ),
            )
            db.execute(
                "UPDATE subagents SET status='cancelled',result=?,finished_at=? "
                "WHERE agent_id=? AND status='waiting'",
                ("Blocked by an unsuccessful workflow dependency", now, node["agent_id"]),
            )
            states[node["node_id"]] = "blocked"
            changed = True
    active = db.execute(
        "SELECT COUNT(*) FROM subagents a WHERE a.user_id=? AND a.status='running' "
        "AND EXISTS(SELECT 1 FROM task_delegations d WHERE d.agent_id=a.agent_id "
        "AND d.run_id=?)",
        (task["user_id"], run["run_id"]),
    ).fetchone()[0]
    from core.subagents.storage import resume

    for node in nodes:
        if active >= MAX_ACTIVE:
            break
        if node["status"] not in {"pending", "paused"}:
            continue
        deps = json.loads(node["depends_on_json"])
        if not all(states[item] == "succeeded" for item in deps):
            continue
        try:
            dependency_outputs = _dependency_outputs(db, run["run_id"], deps)
            turn_id = "workflow:" + run["run_id"] + ":" + node["node_id"]
            if node["started_at"] is not None:
                turn_id += ":resume:" + str(run["control_epoch"])
            resume(
                db,
                task["user_id"],
                {
                    "agent_id": node["agent_id"],
                    "message_id": turn_id,
                    "content": (
                        "Execute this ready workflow node. Use the dependency outputs below as "
                        "input; do not search for them elsewhere.\n\n"
                        + dependency_outputs
                        + "\n\n## Assigned node\n"
                        + node["objective"]
                    ),
                    "success_criteria": node["success_criteria"],
                },
            )
        except ValueError:
            break
        db.execute(
            "UPDATE task_delegations SET state='active' WHERE agent_id=?",
            (node["agent_id"],),
        )
        db.execute(
            "UPDATE task_workflow_nodes SET status='running',started_at=COALESCE(started_at,?) "
            "WHERE run_id=? AND node_id=?",
            (now, run["run_id"], node["node_id"]),
        )
        states[node["node_id"]] = "running"
        active += 1
        changed = True
    nodes = db.execute(
        "SELECT * FROM task_workflow_nodes WHERE run_id=? ORDER BY position", (run["run_id"],)
    ).fetchall()
    terminal = all(node["status"] in TERMINAL for node in nodes)
    if terminal:
        succeeded = all(node["status"] == "succeeded" for node in nodes)
        from core.tasks.storage import event

        if succeeded:
            db.execute(
                "UPDATE task_runs SET status='queued',wait_json=NULL,next_check_at=NULL,"
                "version=version+1 WHERE run_id=? AND status='waiting'",
                (run["run_id"],),
            )
            event(
                db,
                task,
                "workflow_ready",
                {"node_count": len(nodes)},
                db.execute("SELECT * FROM task_runs WHERE run_id=?", (run["run_id"],)).fetchone(),
                wake=wake,
            )
        else:
            db.execute(
                "UPDATE task_runs SET status='paused',resume_status='running',error_json=?,"
                "version=version+1 WHERE run_id=? AND status='waiting'",
                (
                    encode(
                        {
                            "code": "WORKFLOW_FAILED",
                            "message": "One or more workflow nodes did not complete",
                        }
                    ),
                    run["run_id"],
                ),
            )
            db.execute("UPDATE tasks SET needs_attention=1 WHERE task_id=?", (task["task_id"],))
            event(
                db,
                task,
                "blocked",
                {"reason": "One or more workflow nodes did not complete"},
                db.execute("SELECT * FROM task_runs WHERE run_id=?", (run["run_id"],)).fetchone(),
                wake=wake,
            )
        return True
    return changed


def call(db, uid, operation, p):
    if operation == "workflow.define":
        request_id = p.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id is required")
        return define(db, uid, p, request_id)
    if operation == "workflow.get":
        actor = binding(db, uid)
        run = execution(db, uid)
        if not actor or not run:
            raise ValueError("Workflow requires a bound task execution")
        return {
            "nodes": list_nodes(db, run["run_id"]),
            "status": workflow_status(db, run["run_id"]),
        }
    raise ValueError("Unknown workflow operation")
