"""Durable task operations, called exclusively within a Storage transaction."""

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from jsonschema import ValidationError

from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.user import User
from core.storage import outbox
from core.storage.database import encode
from core.tasks.pagination import page
from core.tasks.profiles import allowed
from core.tasks.schedule import next_due
from core.tasks.schema import validate

SPECS = {
    "create": "Create",
    "list": "List",
    "get": "Get",
    "update": "Update",
    "message": "Message",
    "pause": "Control",
    "resume": "Control",
    "cancel": "Control",
    "retry": "Retry",
    "report": "Report",
    "review": "Review",
}
OPEN = "('queued','running','waiting','paused')"
FIELDS = (
    "title",
    "objective",
    "completion",
    "acceptance",
    "plan",
    "context",
    "trigger",
    "executor",
    "policy",
)
COLS = {
    "acceptance": "acceptance_json",
    "plan": "plan_json",
    "context": "context_seed_json",
    "trigger": "trigger_json",
    "executor": "executor_json",
    "policy": "policy_json",
}


def error(code, message, current_revision=None):
    raise ValueError(
        encode(
            {
                "code": code,
                "message": message,
                "retryable": code == "TEMPORARY_UNAVAILABLE",
                "current_revision": current_revision,
            }
        )
    )


def spec(row):
    return {k: json.loads(row[COLS[k]]) if k in COLS else row[k] for k in FIELDS}


def owned(db, uid, tid):
    row = db.execute("SELECT * FROM tasks WHERE user_id=? AND task_id=?", (uid, tid)).fetchone()
    if not row:
        error("NOT_FOUND", "Task not found")
    return row


def iso(value):
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


def view(row):
    return {
        **{k: row[k] for k in ("task_id", "revision", "lifecycle", "close_reason", "title")},
        "needs_attention": bool(row["needs_attention"]),
        "next_due_at": iso(row["next_due_at"]),
        "next_review_at": iso(row["next_review_at"]),
        "updated_at": iso(row["updated_at"]),
        "parent_task_id": row["parent_task_id"],
    }


def runview(db, row):
    if row is None:
        return None
    delivery = db.execute(
        "SELECT status FROM task_deliveries WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
        (row["run_id"],),
    ).fetchone()
    wait = json.loads(row["wait_json"]) if row["wait_json"] else None
    return {
        **{k: row[k] for k in ("run_id", "task_id", "task_revision", "version", "status", "stage")},
        "progress": json.loads(row["progress_json"]).get("progress", ""),
        "due_at": iso(row["due_at"]),
        "started_at": iso(row["started_at"]),
        "finished_at": iso(row["finished_at"]),
        "agent_id": row["agent_id"],
        "wait_reason": wait["condition"]["reason"] if wait else None,
        "wait_kind": wait["condition"]["kind"] if wait else None,
        "next_step": json.loads(row["progress_json"]).get("next_step", ""),
        "next_check_at": iso(row["next_check_at"]),
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error_code": json.loads(row["error_json"]).get("code") if row["error_json"] else None,
        "usage": json.loads(row["usage_json"]),
        "delivery_status": delivery[0] if delivery else "none",
    }


def event(db, task, kind, payload, run=None, wake=False):
    task = owned(db, task["user_id"], task["task_id"])
    if run:
        run = db.execute("SELECT * FROM task_runs WHERE run_id=?", (run["run_id"],)).fetchone()
    key = uuid.uuid4().hex
    db.execute(
        (
            "INSERT INTO task_events(user_id,task_id,run_id,event_key,type,tas"
            "k_revision,run_version,payload_json,created_at) VALUES(?,?,?,?,?,"
            "?,?,?,?)"
        ),
        (
            task["user_id"],
            task["task_id"],
            run["run_id"] if run else None,
            key,
            kind,
            task["revision"],
            run["version"] if run else None,
            encode(payload),
            time.time(),
        ),
    )
    data = {
        "task_id": task["task_id"],
        "revision": task["revision"],
        "run_id": run["run_id"] if run else None,
        **payload,
    }
    if kind == "completed":
        data["delivery_handled"] = True
        data["instruction"] = (
            "The result was already delivered by the task system. Update "
            "related plans silently; do not send the same report again."
        )
    elif kind == "review_required" and "result" in payload:
        data["instruction"] = (
            "Read the task requirements and verify the evidence. Use "
            "task_review to accept or reject. Obtain the user decision "
            "if acceptance.review is user."
        )
    from core.storage.runtime_transactions import append_journal

    append_journal(
        db,
        task["user_id"],
        "task.event",
        {
            "task_id": task["task_id"],
            "run_id": run["run_id"] if run else None,
            "kind": kind,
            "revision": task["revision"],
            "data": payload,
            "state": {
                "lifecycle": task["lifecycle"],
                "run_status": run["status"] if run else None,
                "run_stage": run["stage"] if run else None,
                "run_version": run["version"] if run else None,
                "control_epoch": run["control_epoch"] if run else None,
            },
        },
        key,
    )
    outbox.append(db, task["user_id"], "task." + kind, data, key)
    if wake:
        from core.storage.runtime_transactions import enqueue

        enqueue(db, task["user_id"], Inbound("system", "app.task." + kind, data, key).to_dict())


def check_spec(value):
    validate("Spec", value)
    trigger = value["trigger"]
    if trigger.get("timezone"):
        ZoneInfo(trigger["timezone"])
    ZoneInfo(value["policy"]["task_budget"]["timezone"])
    if value["completion"] == "maintain" and trigger["mode"] not in {"daily", "weekly", "event"}:
        error("INVALID_ARGUMENTS", "Maintain requires recurring or event trigger")
    if value["completion"] == "achieve" and trigger["mode"] in {"daily", "weekly"}:
        error("INVALID_ARGUMENTS", "Recurring tasks require maintain")
    if value["executor"]["kind"] == "program" and (
        value["acceptance"]["review"] != "automatic" or not value["acceptance"]["require_delivery"]
    ):
        error("INVALID_ARGUMENTS", "Program notifications require automatic delivery acceptance")
    if value["executor"]["kind"] == "agent" and value["executor"]["tool_profile"] not in {
        "research_readonly",
        "default",
    }:
        error("INVALID_ARGUMENTS", "Unknown tool profile")
    if value["executor"]["kind"] == "agent" and value["executor"]["model_profile"] != "default":
        error("INVALID_ARGUMENTS", "Only default model profile is configured")
    if (
        value["policy"]["run_budget"]["max_cost_microusd"] is not None
        or value["policy"]["task_budget"]["limits"]["max_cost_microusd"] is not None
    ):
        error("INVALID_ARGUMENTS", "Monetary caps require a configured pricing adapter")


def newrun(db, task, key, due, retry=None):
    now = time.time()
    rid = uuid.uuid4().hex
    value = spec(task)
    deadline = value["policy"]["run_deadline_seconds"]
    floor = db.execute(
        "SELECT COALESCE(MAX(signal_id),0) FROM task_signals WHERE user_id=?", (task["user_id"],)
    ).fetchone()[0]
    db.execute(
        (
            "INSERT INTO task_runs(run_id,user_id,task_id,task_revision,spec_s"
            "napshot_json,occurrence_key,retry_of_run_id,status,due_at,deadlin"
            "e_at,created_at,signal_floor) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
        ),
        (
            rid,
            task["user_id"],
            task["task_id"],
            task["revision"],
            encode(value),
            key,
            retry,
            "queued",
            due,
            due + deadline if deadline else None,
            now,
            floor,
        ),
    )
    return db.execute("SELECT * FROM task_runs WHERE run_id=?", (rid,)).fetchone()


def current(db, tid):
    return db.execute(
        "SELECT * FROM task_runs WHERE task_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (tid,),
    ).fetchone()


def mutate(db, uid, action, p, request):
    now = time.time()
    if action == "create":
        value = {k: p[k] for k in FIELDS}
        check_spec(value)
        known = db.execute(
            "SELECT * FROM tasks WHERE user_id=? AND intent_key=?", (uid, p["intent_key"])
        ).fetchone()
        if known:
            if spec(known) != value:
                error("IDEMPOTENCY_CONFLICT", "Intent already exists with different requirements")
            return (known, "noop")
        parent = p.get("parent_task_id")
        if parent:
            par = owned(db, uid, parent)
            if par["parent_task_id"] or par["lifecycle"] != "active":
                error("INVALID_STATE", "Parent must be an active top-level task")
        tid = uuid.uuid4().hex
        due = next_due(value["trigger"], now)
        db.execute(
            (
                "INSERT INTO tasks(task_id,user_id,intent_key,parent_task_id,lifec"
                "ycle,title,objective,completion,acceptance_json,plan_json,context"
                "_seed_json,trigger_json,executor_json,policy_json,next_due_at,cre"
                "ated_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            ),
            (
                tid,
                uid,
                p["intent_key"],
                parent,
                "active",
                value["title"],
                value["objective"],
                value["completion"],
                encode(value["acceptance"]),
                encode(value["plan"]),
                encode(value["context"]),
                encode(value["trigger"]),
                encode(value["executor"]),
                encode(value["policy"]),
                due,
                now,
                now,
            ),
        )
        task = owned(db, uid, tid)
        event(db, task, "created", {"spec": value})
        return (task, "applied")
    task = owned(db, uid, p["task_id"])
    if task["revision"] != p["expected_revision"]:
        error("REVISION_CONFLICT", "Read the current task before changing it", task["revision"])
    run = current(db, task["task_id"])
    if action == "update":
        if task["lifecycle"] == "closed":
            error("INVALID_STATE", "Task is closed")
        value = {**spec(task), **p["patch"]}
        check_spec(value)
        if (
            p["apply_to"] == "current_and_future"
            and run
            and (run["status"] in {"running", "waiting"})
        ):
            error(
                "INVALID_STATE",
                "Pause and settle current execution before changing its requirements",
            )
        assignments = []
        values = []
        for k, v in value.items():
            assignments.append(COLS.get(k, k) + "=?")
            values.append(encode(v) if k in COLS else v)
        db.execute(
            "UPDATE tasks SET "
            + ",".join(assignments)
            + ",revision=revision+1,next_due_at=?,updated_at=? WHERE task_id=?",
            (*values, next_due(value["trigger"], now), now, task["task_id"]),
        )
        task = owned(db, uid, task["task_id"])
        if p["apply_to"] == "current_and_future" and run and run["status"] in {"paused", "queued"}:
            old_spec = json.loads(run["spec_snapshot_json"])
            if (
                value["executor"] != old_spec["executor"]
                or value["completion"] != old_spec["completion"]
            ):
                error(
                    "INVALID_ARGUMENTS",
                    "Current execution cannot change executor or completion kind",
                )
            if (
                run["context_id"]
                and db.execute(
                    (
                        "SELECT 1 FROM tool_invocations WHERE user_id=? AND status IN "
                        "('running','queued','admitting','waiting_approval')"
                    ),
                    (run["context_id"],),
                ).fetchone()
            ):
                error(
                    "INVALID_STATE",
                    "Outstanding tools must settle before applying new requirements",
                )
            db.execute(
                (
                    "UPDATE task_runs SET "
                    "task_revision=?,spec_snapshot_json=?,control_epoch=control_epoch"
                    "+1,version=version+1,progress_json=? WHERE run_id=?"
                ),
                (
                    task["revision"],
                    encode(value),
                    encode({"progress": "Requirements updated", "next_step": value["objective"]}),
                    run["run_id"],
                ),
            )
        event(db, task, "updated", {"spec": value, "reason": p["reason"]})
    elif action in {"pause", "resume", "cancel"}:
        if task["lifecycle"] == "closed":
            error("INVALID_STATE", "Task is closed")
        if (
            action == "resume"
            and task["parent_task_id"]
            and (owned(db, uid, task["parent_task_id"])["lifecycle"] != "active")
        ):
            error("INVALID_STATE", "Parent is paused")
        if (
            action == "resume"
            and task["lifecycle"] == "active"
            and (not run or run["status"] != "paused")
        ):
            error("INVALID_STATE", "Task is not paused")
        lifecycle = {"pause": "paused", "resume": "active", "cancel": "closed"}[action]
        children = db.execute(
            "SELECT * FROM tasks WHERE user_id=? AND (task_id=? OR parent_task_id=?)",
            (uid, task["task_id"], task["task_id"]),
        ).fetchall()
        for item in children:
            if item["lifecycle"] == "closed":
                continue
            if (
                action == "resume"
                and item["paused_by_task_id"] != task["task_id"]
                and item["task_id"] != task["task_id"]
            ):
                continue
            if action == "pause" and item["lifecycle"] == "paused":
                continue
            db.execute(
                (
                    "UPDATE tasks SET lifecycle=?,close_reason=?,paused_by_task_id=?,r"
                    "evision=revision+1,updated_at=? WHERE task_id=?"
                ),
                (
                    lifecycle,
                    "cancelled" if action == "cancel" else None,
                    task["task_id"] if action == "pause" else None,
                    now,
                    item["task_id"],
                ),
            )
            rows = db.execute(
                f"SELECT * FROM task_runs WHERE task_id=? AND status IN {OPEN}", (item["task_id"],)
            ).fetchall()
            for r in rows:
                from core.tasks.delegation import control_children

                control_children(db, r["run_id"], action)
                status = (
                    "cancelled"
                    if action == "cancel"
                    else "paused"
                    if action == "pause"
                    else "waiting"
                    if r["wait_json"]
                    else "queued"
                )
                db.execute(
                    (
                        "UPDATE task_runs SET status=?,resume_status=?,control_epoch=contr"
                        "ol_epoch+1,version=version+1,lease_until=0,finished_at=? WHERE ru"
                        "n_id=?"
                    ),
                    (
                        status,
                        r["status"] if action == "pause" else None,
                        now if action == "cancel" else None,
                        r["run_id"],
                    ),
                )
                if r["agent_id"]:
                    from core.subagents.storage import finish

                    child = db.execute(
                        "SELECT * FROM subagents WHERE agent_id=?", (r["agent_id"],)
                    ).fetchone()
                    if child["status"] == "running" and action != "resume":
                        finish(db, child, "failed" if action == "pause" else "cancelled", action)
                if action != "resume":
                    db.execute(
                        (
                            "UPDATE task_deliveries SET status='cancelled' WHERE run_id=? AND "
                            "status='pending'"
                        ),
                        (r["run_id"],),
                    )
            if action == "resume":
                db.execute(
                    (
                        "UPDATE task_deliveries SET status='pending' WHERE task_id=? "
                        "AND status='cancelled'"
                    ),
                    (item["task_id"],),
                )
                db.execute("UPDATE tasks SET needs_attention=0 WHERE task_id=?", (item["task_id"],))
            event(db, owned(db, uid, item["task_id"]), action, {"reason": p["reason"]})
        if action == "resume":
            db.execute("UPDATE tasks SET needs_attention=0 WHERE task_id=?", (task["task_id"],))
            db.execute(
                (
                    "UPDATE task_deliveries SET status='pending' WHERE task_id=? AND "
                    "status='cancelled'"
                ),
                (task["task_id"],),
            )
        task = owned(db, uid, task["task_id"])
    elif action == "retry":
        if task["lifecycle"] != "active":
            error("INVALID_STATE", "Task is not active")
        old = db.execute(
            "SELECT * FROM task_runs WHERE user_id=? AND task_id=? AND run_id=?",
            (uid, task["task_id"], p["failed_run_id"]),
        ).fetchone()
        if not old or old["status"] != "failed":
            error("INVALID_STATE", "Only a failed run can be retried")
        if db.execute(
            f"SELECT 1 FROM task_runs WHERE task_id=? AND status IN {OPEN}", (task["task_id"],)
        ).fetchone():
            error("ACTIVE_RUN_EXISTS", "Task already has an open run")
        run = newrun(db, task, "retry:" + request, now, old["run_id"])
        event(db, task, "retried", {"reason": p["reason"]}, run)
    elif action == "review":
        if (
            not run
            or run["run_id"] != p["run_id"]
            or run["stage"] != "verify"
            or run["status"] != "waiting"
        ):
            error("INVALID_STATE", "Run is not awaiting review")
        db.execute("UPDATE tasks SET needs_attention=0 WHERE task_id=?", (task["task_id"],))
        if p["accepted"]:
            result = json.loads(run["result_json"])
            if json.loads(run["spec_snapshot_json"])["acceptance"]["require_delivery"]:
                request_delivery(db, task, run, result)
            else:
                complete(db, task, run, result)
        else:
            db.execute(
                (
                    "UPDATE task_runs SET stage='execute',status='queued',wait_json=NU"
                    "LL,version=version+1,progress_json=? WHERE run_id=?"
                ),
                (
                    encode({"progress": p["reason"], "next_step": "Revise result based on review"}),
                    run["run_id"],
                ),
            )
        event(db, task, "reviewed", {"accepted": p["accepted"], "reason": p["reason"]}, run)
    elif action == "message":
        if task["lifecycle"] == "closed":
            error("INVALID_STATE", "Task is closed")
        if run and p.get("run_id") and p["run_id"] != run["run_id"]:
            error("INVALID_ARGUMENTS", "Message refers to a different run")
        if run and run["status"] not in {"queued", "running", "waiting", "paused"}:
            if p.get("run_id"):
                error("INVALID_STATE", "Execution has ended; send context to the next execution")
            run = None
        event(db, task, "message", p, run)
        if run and run["context_id"]:
            # Persist steering without replacing an active Brain lease. Paused
            # runs retain it in the inbox until their explicit resume.
            from core.tasks.inbox import deliver

            deliver(
                db,
                run["context_id"],
                "message",
                {"content": p["content"], "refs": p["refs"]},
                "task-message:" + request,
            )
        if run and run["status"] == "waiting":
            db.execute(
                "UPDATE task_runs SET progress_json=? WHERE run_id=?",
                (
                    encode(
                        {
                            "progress": json.loads(run["progress_json"]).get("progress", ""),
                            "next_step": p["content"],
                        }
                    ),
                    run["run_id"],
                ),
            )
            wait = json.loads(run["wait_json"])
            if (
                wait["condition"]["kind"] == "user"
                and p.get("reply_to") == wait["wait_id"]
                and run["stage"] != "verify"
            ):
                db.execute(
                    (
                        "UPDATE task_runs SET status='queued',wait_json=NULL,version=versi"
                        "on+1 WHERE run_id=?"
                    ),
                    (run["run_id"],),
                )
                db.execute("UPDATE tasks SET needs_attention=0 WHERE task_id=?", (task["task_id"],))
                event(db, task, "wait_satisfied", {"kind": "user"}, run)
    return (task, "applied")


def call(db, uid, operation, params):
    action = operation.removeprefix("task.")
    if action == "context":
        from core.tasks.entities import binding, execution

        actor = binding(db, uid)
        run = execution(db, uid)
        if not actor:
            return None
        task = owned(db, actor.user_id, actor.task_id)
        return {
            "spec": json.loads(run["spec_snapshot_json"]),
            "memory": json.loads(task["memory_json"]),
            "role": actor.role.value,
            "parent_agent_id": actor.parent_agent_id,
            "run_id": actor.run_id,
            "task_id": actor.task_id,
        }
    if action == "tick":
        return tick(db)
    if action == "guard":
        return guard(db, uid, params)
    if action == "signal":
        return signal(db, uid, params)
    if action.startswith("delivery."):
        return delivery(db, uid, action, params)
    identity = User.from_storage_id(uid)
    if identity.agent_id:
        if action != "report":
            error("FORBIDDEN", "Only task_report is available to task agents")
        request = params.get("request_id")
        if not request:
            error("INVALID_ARGUMENTS", "request_id required")
        payload = {k: v for k, v in params.items() if k not in {"user_id", "request_id", "fence"}}
        fingerprint = hashlib.sha256(encode(payload).encode()).hexdigest()
        known = db.execute(
            "SELECT * FROM task_requests WHERE user_id=? AND request_id=?", (uid, request)
        ).fetchone()
        if known:
            if known["fingerprint"] != fingerprint:
                error("IDEMPOTENCY_CONFLICT", "Report content changed")
            return json.loads(known["response_json"])
        result = report(db, uid, params)
        db.execute(
            "INSERT INTO task_requests VALUES(?,?,?,?,?,?)",
            (uid, request, action, fingerprint, encode(result), time.time()),
        )
        return result
    if action not in SPECS or action == "report":
        error("FORBIDDEN", "Unknown task operation")
    p = {k: v for (k, v) in params.items() if k not in {"user_id", "request_id", "fence"}}
    try:
        validate(SPECS[action], p)
    except ValidationError as exc:
        error("INVALID_ARGUMENTS", exc.message)
    if action == "list":
        rows = db.execute(
            "SELECT * FROM tasks WHERE user_id=? ORDER BY updated_at DESC,task_id", (uid,)
        ).fetchall()
        rows = [
            r
            for r in rows
            if (
                p.get("lifecycle", "active") == "all"
                or r["lifecycle"] == p.get("lifecycle", "active")
            )
            and (
                not p.get("query")
                or p["query"].casefold() in (r["title"] + " " + r["objective"]).casefold()
            )
            and ("needs_attention" not in p or bool(r["needs_attention"]) == p["needs_attention"])
        ]
        rows, cursor = page(rows, uid, p)
        return {"items": [view(r) for r in rows], "next_cursor": cursor}
    if action == "get":
        t = owned(db, uid, p["task_id"])
        r = current(db, t["task_id"])
        runs = []
        events = []
        cursor = None
        if p["view"] == "run":
            r = db.execute(
                "SELECT * FROM task_runs WHERE user_id=? AND task_id=? AND run_id=?",
                (uid, t["task_id"], p["run_id"]),
            ).fetchone()
            if not r:
                error("NOT_FOUND", "Run not found")
        if p["view"] == "runs":
            runs = [
                runview(db, x)
                for x in db.execute(
                    "SELECT * FROM task_runs WHERE task_id=? ORDER BY created_at DESC,run_id DESC",
                    (t["task_id"],),
                )
            ]
        if p["view"] == "events":
            events = [
                {
                    "event_id": x["event_id"],
                    "type": x["type"],
                    "run_id": x["run_id"],
                    "task_revision": x["task_revision"],
                    "summary": json.loads(x["payload_json"]).get("reason", x["type"]),
                    "refs": [],
                    "occurred_at": iso(x["created_at"]),
                }
                for x in db.execute(
                    "SELECT * FROM task_events WHERE task_id=? ORDER BY event_id DESC",
                    (t["task_id"],),
                )
            ]
        if p["view"] == "runs":
            runs, cursor = page(runs, uid, p)
        if p["view"] == "events":
            events, cursor = page(events, uid, p)
        from core.tasks.inspection import inspect_execution

        inspection = (
            {"execution": inspect_execution(db, t, r)}
            if p.get("include_execution", True) and p["view"] in {"detail", "run"}
            else {}
        )
        return {
            "task": view(t),
            **inspection,
            "spec": spec(t),
            "memory": json.loads(t["memory_json"]),
            "run": runview(db, r),
            "runs": runs,
            "events": events,
            "next_cursor": cursor,
        }
    request = params.get("request_id")
    if not request:
        error("INVALID_ARGUMENTS", "request_id required")
    fingerprint = hashlib.sha256(encode([action, p]).encode()).hexdigest()
    known = db.execute(
        "SELECT * FROM task_requests WHERE user_id=? AND request_id=?", (uid, request)
    ).fetchone()
    if known:
        if known["fingerprint"] != fingerprint:
            error("IDEMPOTENCY_CONFLICT", "Request content changed")
        result = json.loads(known["response_json"])
        result["replayed"] = True
        return result
    (task, effect) = mutate(db, uid, action, p, request)
    result = {
        "task": view(task),
        "run": runview(db, current(db, task["task_id"])),
        "effect": effect,
        "replayed": False,
    }
    db.execute(
        "INSERT INTO task_requests VALUES(?,?,?,?,?,?)",
        (uid, request, action, fingerprint, encode(result), time.time()),
    )
    return result


def tick(db):
    now = time.time()
    count = 0
    for delivery_row in db.execute(
        "SELECT * FROM task_deliveries WHERE status='inflight' AND lease_until<=?", (now,)
    ).fetchall():
        db.execute(
            "UPDATE task_deliveries SET status='unknown' WHERE delivery_id=?",
            (delivery_row["delivery_id"],),
        )
        task = owned(db, delivery_row["user_id"], delivery_row["task_id"])
        db.execute("UPDATE tasks SET needs_attention=1 WHERE task_id=?", (task["task_id"],))
        run = db.execute(
            "SELECT * FROM task_runs WHERE run_id=?", (delivery_row["run_id"],)
        ).fetchone()
        event(
            db,
            task,
            "review_required",
            {
                "reason": "Delivery receipt missing; verify before retrying",
                "delivery_id": delivery_row["delivery_id"],
            },
            run,
            True,
        )
    for t in db.execute(
        "SELECT * FROM tasks WHERE lifecycle='active' AND next_due_at<=? LIMIT 128", (now,)
    ).fetchall():
        trigger = json.loads(t["trigger_json"])
        policy = json.loads(t["policy_json"])
        exists = db.execute(
            f"SELECT 1 FROM task_runs WHERE task_id=? AND status IN {OPEN}", (t["task_id"],)
        ).fetchone()
        if not exists and (
            now - t["next_due_at"] <= policy["grace_seconds"] or trigger["mode"] == "now"
        ):
            newrun(db, t, f"{t['revision']}:{t['next_due_at']}", now)
            count += 1
        due = next_due(trigger, now) if trigger["mode"] in {"daily", "weekly"} else None
        db.execute("UPDATE tasks SET next_due_at=? WHERE task_id=?", (due, t["task_id"]))
        if (
            not exists
            and trigger["mode"] == "once"
            and now - t["next_due_at"] > policy["grace_seconds"]
        ):
            db.execute(
                (
                    "UPDATE tasks SET lifecycle='closed',close_reason='expired',r"
                    "evision=revision+1 WHERE task_id=?"
                ),
                (t["task_id"],),
            )
            event(
                db,
                t,
                "expired",
                {"reason": "Scheduled occurrence missed its grace period"},
                wake=True,
            )

    for r in db.execute(
        "SELECT r.* FROM task_runs r JOIN tasks t USING(task_id) WHERE t.l"
        "ifecycle='active' AND r.status IN ('queued','running','waiting') "
        "LIMIT 128"
    ).fetchall():
        t = owned(db, r["user_id"], r["task_id"])
        value = json.loads(r["spec_snapshot_json"])
        if r["deadline_at"] and now > r["deadline_at"]:
            db.execute(
                (
                    "UPDATE task_runs SET status='failed',finished_at=?,error_json=?,v"
                    "ersion=version+1 WHERE run_id=?"
                ),
                (now, encode({"code": "DEADLINE_EXCEEDED"}), r["run_id"]),
            )
            event(db, t, "failed", {"reason": "Deadline exceeded"}, r, True)
            continue
        if r["status"] == "waiting":
            wait = json.loads(r["wait_json"])["condition"]
            ready = False
            if wait["kind"] == "delivery":
                d = db.execute(
                    "SELECT status FROM task_deliveries WHERE delivery_id=?", (wait["delivery_id"],)
                ).fetchone()
                if d and d["status"] == "accepted":
                    result = (
                        json.loads(r["result_json"])
                        if r["result_json"]
                        else {
                            "summary": "Notification accepted",
                            "refs": [],
                            "evidence": [
                                {
                                    "kind": "external",
                                    "ref": wait["delivery_id"],
                                    "summary": "Channel accepted delivery",
                                }
                            ],
                        }
                    )
                    complete(db, t, r, result)
            elif wait["kind"] == "time":
                ready = datetime.fromisoformat(wait["at"].replace("Z", "+00:00")).timestamp() <= now
            elif wait["kind"] == "event":
                ready = r["next_check_at"] is not None and r["next_check_at"] <= now
            elif wait["kind"] == "subagents":
                ids = wait["agent_ids"]
                states = db.execute(
                    (
                        "SELECT a.agent_id,a.status FROM subagents a JOIN "
                        "task_delegations d USING(agent_id) WHERE d.run_id=?"
                    ),
                    (r["run_id"],),
                ).fetchall()
                current_states = {x["agent_id"]: x["status"] for x in states}
                ready = all(
                    current_states.get(i) in {"succeeded", "failed", "cancelled"} for i in ids
                )
            elif wait["kind"] == "tool":
                tool = db.execute(
                    "SELECT status FROM tool_invocations WHERE user_id=? AND invocation_id=?",
                    (r["context_id"], wait["invocation_id"]),
                ).fetchone()
                ready = bool(
                    tool
                    and tool["status"]
                    in {"succeeded", "failed", "timeout", "cancelled", "outcome_unknown"}
                )
            if ready:
                db.execute(
                    (
                        "UPDATE task_runs SET "
                        "status='queued',wait_json=NULL,next_check_at=NULL,version=ve"
                        "rsion+1 WHERE run_id=?"
                    ),
                    (r["run_id"],),
                )
                event(db, t, "wait_satisfied", {"kind": wait["kind"]}, r)
            continue
        if r["status"] == "queued":
            if value["executor"]["kind"] == "program":
                did = uuid.uuid4().hex
                db.execute(
                    (
                        "INSERT INTO task_deliveries(delivery_id,user_id,task_id,run_id,de"
                        "dupe_key,purpose,destination_ref,payload_json,status,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)"
                    ),
                    (
                        did,
                        t["user_id"],
                        t["task_id"],
                        r["run_id"],
                        r["run_id"] + ":result",
                        "reminder",
                        value["policy"]["notify"]["destination_ref"],
                        encode(value["executor"]["params"]),
                        "pending",
                        now,
                    ),
                )
                wait = {
                    "wait_id": uuid.uuid4().hex,
                    "condition": {
                        "kind": "delivery",
                        "reason": "Waiting for delivery",
                        "delivery_id": did,
                    },
                    "signal_floor": r["signal_floor"],
                    "next_check_at": None,
                }
                db.execute(
                    (
                        "UPDATE task_runs SET status='waiting',stage='deliver',wait_json=?"
                        ",version=version+1 WHERE run_id=?"
                    ),
                    (encode(wait), r["run_id"]),
                )
                event(db, t, "delivery_requested", {"delivery_id": did}, r)
            else:
                from core.subagents.storage import resume, spawn

                active = db.execute(
                    (
                        "SELECT count(*) FROM subagents a WHERE a.user_id=? AND "
                        "a.status='running' AND NOT EXISTS(SELECT 1 FROM task_runs "
                        "tr WHERE tr.context_id=a.storage_id AND "
                        "tr.status!='running')"
                    ),
                    (t["user_id"],),
                ).fetchone()[0]
                if active >= 8 and (
                    not r["agent_id"]
                    or db.execute(
                        "SELECT status FROM subagents WHERE agent_id=?", (r["agent_id"],)
                    ).fetchone()[0]
                    != "running"
                ):
                    continue

                if not r["agent_id"]:
                    child = spawn(
                        db,
                        t["user_id"],
                        {
                            "request_id": "task:" + r["run_id"],
                            "title": value["title"],
                            "objective": value["objective"]
                            + "\nBackground: "
                            + encode(value["context"])
                            + "\nCurrent plan: "
                            + encode(value["plan"])
                            + "\nPrevious task notes: "
                            + t["memory_json"]
                            + "\nUse task_report to report progress, waiting, or completion.",
                            "success_criteria": value["acceptance"]["criteria"],
                            "max_steps": min(100, value["policy"]["run_budget"]["max_steps"]),
                            "timeout_seconds": 3600,
                            "notify_parent": False,
                        },
                    )
                    agent = child["agent_id"]
                    context = db.execute(
                        "SELECT storage_id FROM subagents WHERE agent_id=?", (agent,)
                    ).fetchone()[0]
                    db.execute(
                        "UPDATE task_runs SET agent_id=?,context_id=? WHERE run_id=?",
                        (agent, context, r["run_id"]),
                    )
                    from core.tasks.inbox import bind_pending

                    bind_pending(db, t, r, context)
                else:
                    resume(
                        db,
                        t["user_id"],
                        {
                            "agent_id": r["agent_id"],
                            "message_id": uuid.uuid4().hex,
                            "content": "Continue using requirements: "
                            + r["spec_snapshot_json"]
                            + "\nCheckpoint: "
                            + r["progress_json"],
                        },
                    )
                db.execute(
                    (
                        "UPDATE task_runs SET status='running',started_at=COALESCE(started"
                        "_at,?),last_progress_at=?,version=version+1 WHERE run_id=?"
                    ),
                    (now, now, r["run_id"]),
                )
                event(db, t, "run_started", {"resumed": bool(r["agent_id"])}, r)
        elif r["agent_id"] and now - (r["last_progress_at"] or r["started_at"] or now) > value[
            "policy"
        ].get("progress_timeout_seconds", 300):
            outstanding = db.execute(
                (
                    "SELECT 1 FROM tool_invocations WHERE user_id=? AND status "
                    "IN ('running','queued','waiting_approval')"
                ),
                (r["context_id"],),
            ).fetchone()
            if not outstanding:
                db.execute(
                    (
                        "UPDATE task_runs SET "
                        "status='paused',resume_status='running',control_epoch=contro"
                        "l_epoch+1,error_json=?,version=version+1 WHERE run_id=?"
                    ),
                    (encode({"code": "NO_PROGRESS"}), r["run_id"]),
                )
                db.execute("UPDATE tasks SET needs_attention=1 WHERE task_id=?", (t["task_id"],))
                from core.subagents.storage import finish

                child = db.execute(
                    "SELECT * FROM subagents WHERE agent_id=?", (r["agent_id"],)
                ).fetchone()
                if child["status"] == "running":
                    finish(db, child, "failed", "Task needs progress review")
                event(
                    db,
                    t,
                    "review_required",
                    {
                        "reason": (
                            "No progress within the configured interval and no active external work"
                        )
                    },
                    r,
                    True,
                )
        elif r["agent_id"]:
            child = db.execute(
                "SELECT * FROM subagents WHERE agent_id=?", (r["agent_id"],)
            ).fetchone()
            if child["status"] in {"failed", "cancelled", "succeeded"}:
                db.execute(
                    (
                        "UPDATE task_runs SET status='paused',resume_status='running',erro"
                        "r_json=?,version=version+1 WHERE run_id=?"
                    ),
                    (encode({"code": "REPORT_REQUIRED", "message": child["result"]}), r["run_id"]),
                )
                db.execute("UPDATE tasks SET needs_attention=1 WHERE task_id=?", (t["task_id"],))
                event(
                    db,
                    t,
                    "review_required",
                    {"reason": "Agent ended without an accepted report"},
                    r,
                    True,
                )
    return count


def complete(db, t, r, result):
    now = time.time()
    db.execute(
        (
            "UPDATE task_runs SET status='succeeded',wait_json=NULL,result_jso"
            "n=?,finished_at=?,version=version+1 WHERE run_id=?"
        ),
        (encode(result), now, r["run_id"]),
    )
    db.execute(
        "UPDATE tasks SET memory_json=?,needs_attention=0,updated_at=? WHERE task_id=?",
        (
            encode({"summary": result["summary"][:12000], "refs": result.get("refs", [])}),
            now,
            t["task_id"],
        ),
    )
    if t["completion"] == "achieve":
        db.execute(
            (
                "UPDATE tasks SET lifecycle='closed',close_reason='achieved',revis"
                "ion=revision+1,updated_at=? WHERE task_id=?"
            ),
            (now, t["task_id"]),
        )
    event(
        db, t, "completed", {"result": result}, r, json.loads(t["executor_json"])["kind"] == "agent"
    )


def delivery(db, uid, action, p):
    now = time.time()
    if action == "delivery.claim":
        row = db.execute(
            (
                "SELECT d.* FROM task_deliveries d JOIN tasks t USING(task_id) WHE"
                "RE d.user_id=? AND t.lifecycle='active' AND d.status='pending' OR"
                "DER BY d.created_at LIMIT 1"
            ),
            (uid,),
        ).fetchone()
        if not row:
            return None
        db.execute(
            (
                "UPDATE task_deliveries SET status='inflight',lease_owner=?,lease_"
                "token=lease_token+1,lease_until=?,attempt_count=attempt_count+1 W"
                "HERE delivery_id=?"
            ),
            (p["owner"], now + 30, row["delivery_id"]),
        )
        return {
            **dict(row),
            "lease_token": row["lease_token"] + 1,
            "payload": json.loads(row["payload_json"]),
        }
    validate("DeliveryAck", {k: v for (k, v) in p.items() if k != "user_id"})
    row = db.execute(
        "SELECT * FROM task_deliveries WHERE user_id=? AND delivery_id=?", (uid, p["delivery_id"])
    ).fetchone()
    if (
        row
        and row["status"] == "accepted"
        and row["lease_owner"] == p["owner"]
        and row["lease_token"] == p["lease_token"]
    ):
        if row["receipt_json"] != encode(p["receipt"]):
            error("IDEMPOTENCY_CONFLICT", "Receipt changed")
        return {"accepted": True}
    if (
        not row
        or row["lease_owner"] != p["owner"]
        or row["lease_token"] != p["lease_token"]
        or (row["status"] not in {"inflight", "unknown"})
    ):
        error("LEASE_LOST", "Delivery is not owned by caller")
    db.execute(
        "UPDATE task_deliveries SET status=?,receipt_json=?,accepted_at=? WHERE delivery_id=?",
        (
            p["status"],
            encode(p["receipt"]),
            now if p["status"] == "accepted" else None,
            row["delivery_id"],
        ),
    )
    r = db.execute("SELECT * FROM task_runs WHERE run_id=?", (row["run_id"],)).fetchone()
    t = owned(db, uid, row["task_id"])
    if p["status"] == "accepted" and t["lifecycle"] == "active" and (r["status"] == "waiting"):
        result = (
            json.loads(r["result_json"])
            if r["result_json"]
            else {
                "summary": json.loads(row["payload_json"]).get("text", "Delivered"),
                "refs": [],
                "evidence": [
                    {
                        "kind": "external",
                        "ref": row["delivery_id"],
                        "summary": "Channel accepted delivery",
                    }
                ],
            }
        )
        complete(db, t, r, result)
    return {"accepted": True}


def report(db, storage_id, p):
    args = {k: v for (k, v) in p.items() if k not in {"user_id", "request_id", "fence"}}
    validate("Report", args)
    r = db.execute("SELECT * FROM task_runs WHERE context_id=?", (storage_id,)).fetchone()
    if not r or r["status"] != "running":
        error("INVALID_STATE", "Task execution is not running")
    t = owned(db, r["user_id"], r["task_id"])
    if t["lifecycle"] != "active":
        error("INVALID_STATE", "Task is not active")
    decision = args["decision"]
    question = None
    db.execute(
        "UPDATE task_runs SET progress_json=?,last_progress_at=?,version=version+1 WHERE run_id=?",
        (encode(args["checkpoint"]), time.time(), r["run_id"]),
    )
    if (
        decision == "complete"
        and db.execute(
            (
                "SELECT 1 FROM task_delegations d JOIN subagents a "
                "USING(agent_id) WHERE d.run_id=? AND a.status='running'"
            ),
            (r["run_id"],),
        ).fetchone()
    ):
        error("INVALID_STATE", "Wait for owned subagents before completion")
    if decision == "wait" and args["wait"]["kind"] == "subagents":
        for child_id in args["wait"]["agent_ids"]:
            if not db.execute(
                "SELECT 1 FROM task_delegations WHERE run_id=? AND agent_id=?",
                (r["run_id"], child_id),
            ).fetchone():
                error("FORBIDDEN", "Cannot wait for unrelated subagent")
    if decision == "complete":
        db.execute(
            (
                "UPDATE task_runs SET status='waiting',stage='verify',result_json="
                "?,wait_json=? WHERE run_id=?"
            ),
            (
                encode(args["result"]),
                encode(
                    {
                        "wait_id": uuid.uuid4().hex,
                        "condition": {
                            "kind": "user",
                            "reason": "Review required",
                            "question": "Does this result satisfy the task?",
                        },
                        "signal_floor": r["signal_floor"],
                        "next_check_at": None,
                    }
                ),
                r["run_id"],
            ),
        )
        if json.loads(r["spec_snapshot_json"])["acceptance"]["review"] == "user":
            db.execute("UPDATE tasks SET needs_attention=1 WHERE task_id=?", (t["task_id"],))
        event(
            db,
            t,
            "review_required",
            {"result": args["result"], "reason": "Result needs verification"},
            r,
            True,
        )
    elif decision == "wait":
        question = uuid.uuid4().hex
        wait = {
            "wait_id": question,
            "condition": args["wait"],
            "signal_floor": r["signal_floor"],
            "next_check_at": args["wait"].get("next_check_at"),
        }
        db.execute(
            "UPDATE task_runs SET status='waiting',wait_json=? WHERE run_id=?",
            (encode(wait), r["run_id"]),
        )
        when = args["wait"].get("at") or args["wait"].get("next_check_at")
        if when:
            due = datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp()
            db.execute("UPDATE task_runs SET next_check_at=? WHERE run_id=?", (due, r["run_id"]))
        if args["wait"]["kind"] == "user":
            db.execute("UPDATE tasks SET needs_attention=1 WHERE task_id=?", (t["task_id"],))
            event(
                db,
                t,
                "input_required",
                {"question_id": question, "question": args["wait"]["question"]},
                r,
                True,
            )
        if args["wait"]["kind"] == "event":
            earlier = db.execute(
                (
                    "SELECT signal_id FROM task_signals WHERE user_id=? AND signal_id>"
                    "? AND source=? AND correlation_key=? AND NOT EXISTS "
                    "(SELECT 1 FROM task_events e WHERE e.run_id=? AND "
                    "e.signal_id=task_signals.signal_id) "
                    "ORDER BY signal_id LIMIT 1"
                ),
                (
                    r["user_id"],
                    r["signal_floor"],
                    args["wait"]["source"],
                    args["wait"]["key"],
                    r["run_id"],
                ),
            ).fetchone()
            if earlier:
                consume_signal(db, t, r, earlier["signal_id"])
                db.execute(
                    (
                        "UPDATE task_runs SET status='queued',wait_json=NULL,version=versi"
                        "on+1 WHERE run_id=?"
                    ),
                    (r["run_id"],),
                )
    elif decision == "blocked":
        db.execute(
            "UPDATE task_runs SET status='paused',resume_status='running' WHERE run_id=?",
            (r["run_id"],),
        )
        db.execute("UPDATE tasks SET needs_attention=1 WHERE task_id=?", (t["task_id"],))
        event(db, t, "review_required", {"reason": args["block_reason"]}, r, True)
    event(db, t, "progress", args, r)
    updated = db.execute("SELECT * FROM task_runs WHERE run_id=?", (r["run_id"],)).fetchone()
    return {
        "accepted": True,
        "run_id": r["run_id"],
        "run_version": updated["version"],
        "status": updated["status"],
        "next_check_at": iso(updated["next_check_at"]),
        "question_id": question,
    }


def request_delivery(db, task, run, result):
    did = uuid.uuid4().hex
    value = json.loads(run["spec_snapshot_json"])
    db.execute(
        (
            "INSERT INTO task_deliveries(delivery_id,user_id,task_id,run_id,de"
            "dupe_key,purpose,destination_ref,payload_json,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)"
        ),
        (
            did,
            task["user_id"],
            task["task_id"],
            run["run_id"],
            run["run_id"] + ":result",
            "result",
            value["policy"]["notify"]["destination_ref"],
            encode({"text": result["summary"], "refs": result["refs"]}),
            "pending",
            time.time(),
        ),
    )
    wait = {
        "wait_id": uuid.uuid4().hex,
        "condition": {
            "kind": "delivery",
            "reason": "Waiting for channel receipt",
            "delivery_id": did,
        },
        "signal_floor": run["signal_floor"],
        "next_check_at": None,
    }
    db.execute(
        (
            "UPDATE task_runs SET stage='deliver',status='waiting',wait_json=?"
            ",version=version+1 WHERE run_id=?"
        ),
        (encode(wait), run["run_id"]),
    )
    event(db, task, "delivery_requested", {"delivery_id": did}, run)


def guard(db, uid, p):
    from core.tasks.entities import AgentRole, binding, execution

    actor = binding(db, uid)
    r = execution(db, uid)
    if not actor:
        return None
    t = owned(db, actor.user_id, actor.task_id)
    agent = db.execute(
        "SELECT status FROM subagents WHERE agent_id=?", (actor.agent_id,)
    ).fetchone()
    allowed_states = {"running", "waiting"} if actor.role == AgentRole.SUBAGENT else {"running"}
    if (
        r["status"] not in allowed_states
        or t["lifecycle"] != "active"
        or agent["status"] != "running"
    ):
        error("INVALID_STATE", "Task execution is stopped")
    if actor.role == AgentRole.SUBAGENT and (
        (p["tool"].startswith("subagent_") and p["tool"] != "subagent_notify")
        or p["tool"] == "task_report"
    ):
        error("FORBIDDEN", "Leaf subagents cannot delegate or report for the parent task")
    if p.get("expected_epoch") is not None and p["expected_epoch"] != r["control_epoch"]:
        error("LEASE_LOST", "Execution replaced")
    profile = json.loads(r["spec_snapshot_json"])["executor"]["tool_profile"]
    management = p["tool"] in {
        "subagent_spawn",
        "subagent_get",
        "subagent_list",
        "subagent_cancel",
        "subagent_continue",
    }
    if not (
        (actor.role == AgentRole.TASK_AGENT and management)
        or (actor.role == AgentRole.SUBAGENT and p["tool"] == "subagent_notify")
        or allowed(profile, p["tool"])
    ):
        error("FORBIDDEN", "Tool outside task profile")
    return {"run_id": r["run_id"], "control_epoch": r["control_epoch"], "role": actor.role.value}


def signal(db, uid, p):
    args = {k: v for k, v in p.items() if k != "user_id"}
    validate("Signal", args)
    now = time.time()
    known = db.execute(
        "SELECT * FROM task_signals WHERE user_id=? AND source=? AND event_key=?",
        (uid, p["source"], p["event_key"]),
    ).fetchone()
    if known:
        if known["payload_json"] != encode(p["payload"]) or known["correlation_key"] != p["key"]:
            error("IDEMPOTENCY_CONFLICT", "Signal content changed")
        return {"signal_id": known["signal_id"]}
    cursor = db.execute(
        (
            "INSERT INTO task_signals(user_id,source,event_key,correlation_key"
            ",payload_json,occurred_at,received_at) VALUES(?,?,?,?,?,?,?)"
        ),
        (
            uid,
            p["source"],
            p["event_key"],
            p["key"],
            encode(p["payload"]),
            datetime.fromisoformat(p["occurred_at"].replace("Z", "+00:00")).timestamp(),
            now,
        ),
    )
    sid = cursor.lastrowid
    tasks = db.execute(
        "SELECT * FROM tasks WHERE user_id=? AND lifecycle='active'", (uid,)
    ).fetchall()
    for t in tasks:
        trigger = json.loads(t["trigger_json"])
        running = db.execute(
            f"SELECT * FROM task_runs WHERE task_id=? AND status IN {OPEN}", (t["task_id"],)
        ).fetchone()
        if running and running["status"] == "waiting":
            wait = json.loads(running["wait_json"])["condition"]
            if (
                wait["kind"] == "event"
                and wait["source"] == p["source"]
                and wait["key"] == p["key"]
            ):
                db.execute(
                    (
                        "UPDATE task_runs SET status='queued',wait_json=NULL,version=versi"
                        "on+1 WHERE run_id=?"
                    ),
                    (running["run_id"],),
                )
                consume_signal(db, t, running, sid)
                event(db, t, "resumed", {"signal_id": sid}, running)
        elif (
            not running
            and trigger["mode"] == "event"
            and trigger["source"] == p["source"]
            and trigger["key"] == p["key"]
        ):
            newrun(db, t, p["source"] + ":" + p["event_key"], now)
    return {"signal_id": sid}


def consume_signal(db, task, run, sid):
    db.execute(
        (
            "INSERT INTO task_events(user_id,task_id,run_id,signal_id,event_k"
            "ey,type,task_revision,run_version,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)"
        ),
        (
            task["user_id"],
            task["task_id"],
            run["run_id"],
            sid,
            f"signal:{run['run_id']}:{sid}",
            "signal_consumed",
            task["revision"],
            run["version"],
            "{}",
            time.time(),
        ),
    )
