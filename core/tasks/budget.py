"""Enforce task model-step and active-time bounds before the next Brain claim."""

import json

from core.storage.database import encode


def admit_step(db, context_id):
    from core.tasks.entities import execution

    run = execution(db, context_id)
    if not run:
        return True
    value = json.loads(run["spec_snapshot_json"])
    budget = value["policy"]["run_budget"]
    usage = json.loads(run["usage_json"])
    # Provider usage recorded in durable completed steps is the source of token accounting.
    tokens = 0
    for row in db.execute(
        (
            "SELECT body_json FROM steps WHERE user_id=? OR user_id IN "
            "(SELECT a.storage_id FROM subagents a JOIN task_delegations "
            "d USING(agent_id) WHERE d.run_id=?)"
        ),
        (run["context_id"], run["run_id"]),
    ):
        response = json.loads(row[0]).get("response") or {}
        u = response.get("usage") or {}
        tokens += u.get("total_tokens", u.get("prompt_tokens", 0) + u.get("completion_tokens", 0))
    usage["tokens"] = max(usage["tokens"], tokens)
    limit = usage["steps"] >= budget["max_steps"] or usage["tokens"] >= budget["max_tokens"]
    # Active duration is bounded per acquired model interval, excluding waiting intervals.
    if usage["active_seconds"] >= budget["max_active_seconds"]:
        limit = True
    from datetime import datetime
    from zoneinfo import ZoneInfo

    task = db.execute("SELECT * FROM tasks WHERE task_id=?", (run["task_id"],)).fetchone()
    for tid in [run["task_id"], task["parent_task_id"]]:
        if not tid:
            continue
        parent = db.execute("SELECT policy_json FROM tasks WHERE task_id=?", (tid,)).fetchone()
        task_budget = json.loads(parent[0])["task_budget"]
        local = datetime.now(ZoneInfo(task_budget["timezone"]))
        start = 0
        if task_budget["window"] == "day":
            start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        elif task_budget["window"] == "month":
            start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        totals = {"steps": 0, "tokens": 0, "active_seconds": 0}
        for item in db.execute(
            (
                "SELECT usage_json FROM task_runs WHERE (task_id=? OR task_id IN "
                "(SELECT task_id FROM tasks WHERE parent_task_id=?)) AND "
                "created_at>=?"
            ),
            (tid, tid, start),
        ):
            used = json.loads(item[0])
            for key in totals:
                totals[key] += used.get(key, 0)
        limits = task_budget["limits"]
        limit = limit or any(totals[k] >= limits["max_" + k] for k in totals)
    if limit:
        db.execute(
            (
                "UPDATE task_runs SET "
                "status='paused',resume_status='running',error_json=?,version=ver"
                "sion+1 WHERE run_id=?"
            ),
            (encode({"code": "BUDGET_EXHAUSTED"}), run["run_id"]),
        )
        db.execute("UPDATE tasks SET needs_attention=1 WHERE task_id=?", (run["task_id"],))
        from core.tasks.storage import event

        event(db, task, "budget_exhausted", {"code": "BUDGET_EXHAUSTED"}, run, wake=True)
        return False
    usage["steps"] += 1
    db.execute("UPDATE task_runs SET usage_json=? WHERE run_id=?", (encode(usage), run["run_id"]))
    return True


def finish_step(db, context_id, elapsed):
    from core.tasks.entities import execution

    row = execution(db, context_id)
    if row:
        usage = json.loads(row["usage_json"])
        usage["active_seconds"] += max(0, elapsed)
        db.execute(
            "UPDATE task_runs SET usage_json=? WHERE run_id=?", (encode(usage), row["run_id"])
        )
