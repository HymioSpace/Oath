"""Durable peer collaboration for Agent Team tasks."""

import json
import time

from core.tasks.entities import binding, execution
from core.tasks.modes import execution_mode


def _context(db, uid):
    actor = binding(db, uid)
    run = execution(db, uid)
    if not actor or not run:
        raise ValueError("Agent Team tools require a bound task execution")
    task = db.execute(
        "SELECT * FROM tasks WHERE user_id=? AND task_id=?", (actor.user_id, actor.task_id)
    ).fetchone()
    snapshot = json.loads(
        db.execute(
            "SELECT spec_snapshot_json FROM task_runs WHERE run_id=?", (run["run_id"],)
        ).fetchone()["spec_snapshot_json"]
    )
    mode = execution_mode(snapshot["executor"])
    if mode != "agent_team":
        raise ValueError("Agent Team tools are only available in agent_team mode")
    if run["status"] not in {"running", "waiting"} or task["lifecycle"] != "active":
        raise ValueError("Agent Team execution is not active")
    return actor, run, task


def _members(db, actor, run):
    leader = run["agent_id"]
    values = []
    if leader:
        row = db.execute(
            "SELECT agent_id,title,status,result FROM subagents WHERE agent_id=?", (leader,)
        ).fetchone()
        if row:
            values.append({**dict(row), "role": "leader"})
    for row in db.execute(
        "SELECT a.agent_id,a.title,a.status,a.result,d.role FROM subagents a "
        "JOIN task_delegations d USING(agent_id) WHERE d.run_id=? ORDER BY d.created_at",
        (run["run_id"],),
    ):
        values.append({**dict(row), "role": row["role"]})
    return values


def members(db, uid, p):
    actor, run, _ = _context(db, uid)
    return _members(db, actor, run)


def send(db, uid, p):
    actor, run, task = _context(db, uid)
    content = p.get("content")
    if not isinstance(content, str) or not 1 <= len(content.strip()) <= 4000:
        raise ValueError("Team message must contain 1..4000 characters")
    request_id = p.get("request_id")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
        raise ValueError("request_id is required")
    roster = _members(db, actor, run)
    allowed = {
        item["agent_id"]: item
        for item in roster
        if item["agent_id"] != actor.agent_id and item["role"] in {"leader", "teammate"}
    }
    recipient = p.get("recipient_agent_id") or ""
    recipients = [recipient] if recipient else list(allowed)
    if not recipients or any(item not in allowed for item in recipients):
        raise ValueError("Recipient is not a teammate in this TaskRun")
    if len(recipients) > 8:
        raise ValueError("Too many Team message recipients")
    message_ids = (
        [request_id]
        if len(recipients) == 1
        else [f"{request_id}:{index}" for index in range(len(recipients))]
    )
    known = [
        db.execute(
            "SELECT * FROM task_team_messages WHERE user_id=? AND message_id=?",
            (actor.user_id, message_id),
        ).fetchone()
        for message_id in message_ids
    ]
    if any(known):
        if not all(known):
            raise ValueError("Team message idempotency conflict")
        if any(
            row["content"] != content or row["recipient_agent_id"] != recipient
            for row, recipient in zip(known, recipients, strict=True)
        ):
            raise ValueError("Team message idempotency conflict")
        values = [_message(row) for row in known]
        return values[0] if len(values) == 1 else {"messages": values}
    created = time.time()
    messages = []
    for message_id, recipient_id in zip(message_ids, recipients, strict=True):
        db.execute(
            "INSERT INTO task_team_messages(message_id,user_id,task_id,run_id,"
            "sender_agent_id,recipient_agent_id,content,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                message_id,
                actor.user_id,
                actor.task_id,
                actor.run_id,
                actor.agent_id,
                recipient_id,
                content,
                created,
            ),
        )
        target = db.execute(
            "SELECT storage_id FROM subagents WHERE agent_id=?", (recipient_id,)
        ).fetchone()
        from core.tasks.inbox import deliver

        deliver(
            db,
            target["storage_id"],
            "team.message",
            {
                "message_id": message_id,
                "sender_agent_id": actor.agent_id,
                "recipient_agent_id": recipient_id,
                "content": content,
            },
            "team-message:" + actor.user_id + ":" + message_id,
        )
        row = db.execute(
            "SELECT * FROM task_team_messages WHERE user_id=? AND message_id=?",
            (actor.user_id, message_id),
        ).fetchone()
        messages.append(_message(row))
    from core.tasks.storage import event

    event(
        db,
        task,
        "team.message",
        {
            "sender_agent_id": actor.agent_id,
            "recipient_agent_ids": recipients,
            "content": content[:1000],
        },
        run,
    )
    return messages[0] if len(messages) == 1 else {"messages": messages}


def inbox(db, uid, p):
    actor, run, _ = _context(db, uid)
    limit = p.get("limit", 50)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    rows = db.execute(
        "SELECT * FROM task_team_messages WHERE run_id=? AND "
        "(sender_agent_id=? OR recipient_agent_id=?) ORDER BY created_at DESC LIMIT ?",
        (run["run_id"], actor.agent_id, actor.agent_id, limit),
    ).fetchall()
    return [_message(row) for row in reversed(rows)]


def all_messages(db, run_id):
    return [
        _message(row)
        for row in db.execute(
            "SELECT * FROM task_team_messages WHERE run_id=? ORDER BY created_at,message_id",
            (run_id,),
        )
    ]


def _message(row):
    return {
        key: row[key]
        for key in (
            "message_id",
            "sender_agent_id",
            "recipient_agent_id",
            "content",
            "created_at",
        )
    }


def call(db, uid, operation, p):
    if operation == "team.send":
        return send(db, uid, p)
    if operation == "team.inbox":
        return inbox(db, uid, p)
    if operation == "team.members":
        return members(db, uid, p)
    raise ValueError("Unknown Agent Team operation")
