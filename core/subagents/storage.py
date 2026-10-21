"""Delegation state transitions; called inside the Storage transaction."""

import json
import time
import uuid

from core.protocol.datatype.inbound import Inbound
from core.protocol.datatype.user import User
from core.storage import outbox, scheduling
from core.storage.database import Database, encode

ACTIVE = {"running"}


def lookup(db, storage_id):
    return db.execute("SELECT * FROM subagents WHERE storage_id=?", (storage_id,)).fetchone()


def get(db, user_id, agent_id):
    row = db.execute(
        "SELECT * FROM subagents WHERE user_id=? AND agent_id=?", (user_id, agent_id)
    ).fetchone()
    if not row:
        raise ValueError("Subagent not found")
    value = dict(row)
    value.pop("storage_id")
    value["notes"] = [
        dict(n)
        for n in db.execute(
            "SELECT note_id,content,created_at FROM subagent_notes "
            "WHERE agent_id=? ORDER BY created_at",
            (agent_id,),
        )
    ]
    return value


def notify(db, row, type_, payload, key):
    from core.storage.runtime_transactions import enqueue

    enqueue(
        db,
        row["user_id"],
        Inbound(
            "system", type_, {"agent_id": row["agent_id"], "title": row["title"], **payload}, key
        ).to_dict(),
    )
    outbox.append(
        db,
        row["user_id"],
        "subagent.status",
        {"agent_id": row["agent_id"], "title": row["title"], **payload},
        key,
    )


def finish(db, row, status, result):
    if row["status"] not in ACTIVE:
        return
    db.execute(
        "UPDATE subagents SET status=?,result=?,finished_at=? WHERE agent_id=?",
        (status, result[:16000], time.time(), row["agent_id"]),
    )
    # Tools not yet started observe the same durable cancellation marker as run.cancel.
    snapshot = db.execute(
        "SELECT version,state_json FROM users WHERE user_id=?", (row["storage_id"],)
    ).fetchone()
    state = json.loads(snapshot["state_json"])
    if state["meta"].get("run_id"):
        from core.storage.runtime_transactions import commit

        state["meta"]["cancelled:" + state["meta"]["run_id"]] = True
        commit(
            db,
            row["storage_id"],
            snapshot["version"],
            state,
            "subagent.terminated",
            "subagent:" + row["agent_id"] + ":terminated",
        )
    db.execute("DELETE FROM inbox_events WHERE user_id=?", (row["storage_id"],))
    db.execute("UPDATE scheduling SET completed=requested WHERE user_id=?", (row["storage_id"],))
    notify(
        db,
        row,
        "subagent.completed",
        {"status": status, "result": result[:16000]},
        "subagent:" + row["agent_id"] + ":completed",
    )


def spawn(db, owner, p):
    if User.from_storage_id(owner).agent_id:
        raise ValueError("Subagents cannot delegate")
    required = ("request_id", "title", "objective", "success_criteria")
    if any(not isinstance(p.get(k), str) or not p[k].strip() for k in required):
        raise ValueError("request_id, title, objective and success_criteria are required")
    if any(len(p[k]) > 20000 for k in required):
        raise ValueError("Delegation description too long")
    known = db.execute(
        "SELECT agent_id FROM subagents WHERE user_id=? AND request_id=?", (owner, p["request_id"])
    ).fetchone()
    if known:
        result = get(db, owner, known[0])
        if any(result[k] != p[k] for k in ("title", "objective", "success_criteria")):
            raise ValueError("Delegation idempotency conflict")
        return result
    if (
        db.execute(
            "SELECT COUNT(*) FROM subagents WHERE user_id=? AND status='running'", (owner,)
        ).fetchone()[0]
        >= 8
    ):
        raise ValueError("At most eight active subagents per user")
    max_steps, timeout = int(p.get("max_steps", 40)), int(p.get("timeout_seconds", 900))
    if not 1 <= max_steps <= 100 or not 10 <= timeout <= 3600:
        raise ValueError("max_steps must be 1..100; timeout_seconds must be 10..3600")
    agent_id = uuid.uuid4().hex
    target = User(owner, agent_id).storage_id
    now = time.time()
    db.execute(
        "INSERT INTO subagents(agent_id,user_id,storage_id,request_id,title,objective,"
        "success_criteria,model,status,max_steps,deadline,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            agent_id,
            owner,
            target,
            p["request_id"],
            p["title"],
            p["objective"],
            p["success_criteria"],
            p.get("model", ""),
            "running",
            max_steps,
            now + timeout,
            now,
        ),
    )
    Database.ensure(db, target)
    parent = json.loads(
        db.execute("SELECT state_json FROM users WHERE user_id=?", (owner,)).fetchone()[0]
    )
    state = json.loads(
        db.execute("SELECT state_json FROM users WHERE user_id=?", (target,)).fetchone()[0]
    )
    if "user_context" in parent["meta"]:
        state["meta"]["user_context"] = parent["meta"]["user_context"]
    model = p.get("model") or parent["meta"].get("selected_model", "")
    if model:
        state["meta"]["selected_model"] = model
        db.execute("UPDATE subagents SET model=? WHERE agent_id=?", (model, agent_id))
    db.execute("UPDATE users SET state_json=? WHERE user_id=?", (encode(state), target))
    item = Inbound(
        "tool",
        "subagent.start",
        {
            "content": "Delegated objective: "
            + p["objective"]
            + "\nSuccess criteria: "
            + p["success_criteria"]
        },
        "subagent:" + agent_id + ":start",
    )
    db.execute(
        "INSERT INTO inbox_events(user_id,kind,identity,item_json) VALUES(?,?,?,?)",
        (target, "tool", item.identity, encode(item.to_dict())),
    )
    scheduling.wake(db, target)
    return get(db, owner, agent_id)


def call(db, uid, operation, p):
    user = User.from_storage_id(uid)
    if operation == "subagent.notify":
        row = lookup(db, uid)
        if not row or row["status"] not in ACTIVE:
            raise ValueError("Only active subagents can notify")
        content, key = p["content"], p["note_id"]
        if not isinstance(content, str) or not 1 <= len(content) <= 4000:
            raise ValueError("Finding must contain 1..4000 characters")
        old = db.execute(
            "SELECT content FROM subagent_notes WHERE agent_id=? AND note_id=?",
            (row["agent_id"], key),
        ).fetchone()
        if old:
            if old[0] != content:
                raise ValueError("Finding idempotency conflict")
            return {"delivered": True}
        if (
            db.execute(
                "SELECT COUNT(*) FROM subagent_notes WHERE agent_id=?", (row["agent_id"],)
            ).fetchone()[0]
            >= 100
        ):
            raise ValueError("At most 100 findings per execution")
        db.execute(
            "INSERT INTO subagent_notes VALUES(?,?,?,?)",
            (row["agent_id"], key, content, time.time()),
        )
        notify(
            db,
            row,
            "subagent.progress",
            {"content": content, "note_id": key},
            "subagent:" + row["agent_id"] + ":" + key,
        )
        return {"delivered": True}
    if user.agent_id:
        raise ValueError("Subagents cannot manage delegated executions")
    if operation == "subagent.spawn":
        return spawn(db, uid, p)
    if operation == "subagent.list":
        return [
            get(db, uid, r[0])
            for r in db.execute(
                "SELECT agent_id FROM subagents WHERE user_id=? ORDER BY created_at DESC LIMIT 100",
                (uid,),
            )
        ]
    if operation == "subagent.get":
        agent_id = p.get("agent_id")
        if not agent_id and p.get("request_id"):
            found = db.execute(
                "SELECT agent_id FROM subagents WHERE user_id=? AND request_id=?",
                (uid, p["request_id"]),
            ).fetchone()
            if found:
                agent_id = found[0]
        return get(db, uid, agent_id)
    if operation == "subagent.cancel":
        row = db.execute(
            "SELECT * FROM subagents WHERE user_id=? AND agent_id=?", (uid, p["agent_id"])
        ).fetchone()
        if not row:
            raise ValueError("Subagent not found")
        finish(db, row, "cancelled", "Cancelled by parent")
        db.execute(
            "UPDATE leases SET expires=0 WHERE user_id=? AND lane='brain'", (row["storage_id"],)
        )
        return get(db, uid, p["agent_id"])
    raise ValueError("Unknown subagent operation")


def project(db, uid, changes, state=None):
    row = lookup(db, uid)
    if not row or row["status"] not in ACTIVE:
        return
    if changes.get("meta", {}).get("tool_wake_pause"):
        finish(db, row, "failed", "Tool continuation budget exhausted")
        return
    for step in changes.get("steps", {}).values():
        if step["status"] in {"failed", "paused", "cancelled"}:
            finish(db, row, "failed", step.get("error") or "Execution stopped")
        elif step["status"] == "completed" and not (step.get("response") or {}).get("tool_calls"):
            if state is None:
                state = json.loads(
                    db.execute("SELECT state_json FROM users WHERE user_id=?", (uid,)).fetchone()[0]
                )
            seen = {
                entry["item"].get("source_id")
                for entry in state["inputs"].values()
                if entry["queue"] == "tool" and entry["item"].get("source") == "tool.terminal"
            }
            if any(
                not call.get("terminal") or call["terminal"]["event_id"] not in seen
                for call in state["calls"].values()
            ):
                continue
            content = (step.get("response") or {}).get(
                "content"
            ) or "Completed without a text result"
            finish(db, row, "succeeded", str(content))


def expire(db):
    for row in db.execute(
        "SELECT * FROM subagents WHERE status='running' AND deadline<=?", (time.time(),)
    ).fetchall():
        finish(db, row, "failed", "Execution deadline exceeded")
        db.execute(
            "UPDATE leases SET expires=0 WHERE user_id=? AND lane='brain'", (row["storage_id"],)
        )
