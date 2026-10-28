"""运行状态、Journal、工具结果和客户端事件的原子提交。"""

import hashlib
import json

from core.protocol.datatype.inbound import Inbound
from core.protocol.interface.storage import StorageConflict
from core.storage import outbox, request_archives, scheduling
from core.storage.database import TABLES, encode
from core.utils.encoding import utc_now_iso


def enqueue(db, user_id, item):
    inbound = Inbound.from_dict(item)
    if inbound.kind not in {"user", "tool", "system"} or not inbound.source_id:
        raise ValueError("queue event needs kind and source_id")
    from core.subagents.storage import lookup

    child = lookup(db, user_id)
    if child and inbound.kind != "tool":
        raise ValueError("Subagents accept only tool results")
    # Receipt time and causal tracing do not change a retried business command.
    fingerprint = encode({k: v for k, v in item.items() if k not in {"occur_dt", "telemetry"}})
    known = db.execute(
        "SELECT id,fingerprint,item_json FROM broker_outbox WHERE user_id=? AND identity=?",
        (user_id, inbound.identity),
    ).fetchone()
    if known:
        original = json.loads(known["item_json"])
        original_fingerprint = encode(
            {k: v for k, v in original.items() if k not in {"occur_dt", "telemetry"}}
        )
        if original_fingerprint != fingerprint:
            raise ValueError("Input idempotency conflict")
        return known["id"]
    return db.execute(
        "INSERT INTO broker_outbox(user_id,kind,identity,item_json,fingerprint) VALUES(?,?,?,?,?)",
        (user_id, inbound.kind, inbound.identity, encode(item), fingerprint),
    ).lastrowid


def append_journal(db, user_id, type_, payload, key, occur_dt=None):
    known = db.execute(
        "SELECT seq,record_json FROM journal_events WHERE user_id=? AND idempotency_key=?",
        (user_id, key),
    ).fetchone()
    if known:
        raw = json.loads(known["record_json"])
        if raw["type"] != type_ or raw["payload"] != payload:
            raise ValueError("Journal idempotency conflict")
        return {"seq": known["seq"], "raw": raw, "hit": True}
    db.execute("UPDATE users SET journal_seq=journal_seq+1 WHERE user_id=?", (user_id,))
    seq = db.execute("SELECT journal_seq FROM users WHERE user_id=?", (user_id,)).fetchone()[0]
    raw = {
        "user_id": user_id,
        "type": type_,
        "payload": payload,
        "payload_schema_version": 1,
        "idempotency_key": key,
        "occur_dt": occur_dt,
        "commit_dt": utc_now_iso(),
    }
    db.execute("INSERT INTO journal_events VALUES(?,?,?,?)", (user_id, seq, key, encode(raw)))
    return {"seq": seq, "raw": raw, "hit": False}


def commit(db, user_id, expected, after, reason, commit_id, fence=None, *, changes=None):
    scheduling.check(db, user_id, fence, required=reason.startswith("step."))
    known = db.execute(
        "SELECT fingerprint FROM commits WHERE user_id=? AND commit_id=?", (user_id, commit_id)
    ).fetchone()
    patch_fingerprint = None
    if changes is not None:
        if (
            not isinstance(changes, dict)
            or not changes.keys() <= set(TABLES)
            or any(not isinstance(entries, dict) for entries in changes.values())
        ):
            raise ValueError("Invalid state patch")
        patch_fingerprint = hashlib.sha256(
            encode(["patch", expected, changes, reason]).encode()
        ).hexdigest()
        if known:
            if known[0] != patch_fingerprint:
                raise ValueError("Commit identity conflict")
            row = db.execute("SELECT version FROM users WHERE user_id=?", (user_id,)).fetchone()
            return {"version": row[0], "archived_requests": {}, "replayed": True}
    row = None
    if not known:
        row = db.execute(
            "SELECT version,state_json FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row["version"] != expected:
            raise StorageConflict("state version changed")
    before = json.loads(row["state_json"]) if row else None
    if changes is not None:
        after = {table: {**before[table], **changes.get(table, {})} for table in TABLES}
        # Request compaction mutates step entries. Keep the previous view intact
        # for journal diffs and outbox projection.
        after["steps"] = {key: dict(step) for key, step in after["steps"].items()}
    archiving = [
        key
        for key, step in after["steps"].items()
        if step.get("status") == "completed" and step.get("request") is not None
    ]
    archiving_drafts = [
        key
        for key, step in after["steps"].items()
        if "active_step" in after.get("meta", {})
        and step.get("request_draft") is not None
        and key != after.get("meta", {}).get("active_step")
        and step.get("status") in {"paused", "failed", "completed"}
    ]
    request_archives.compact(db, user_id, after)
    fingerprint = (
        patch_fingerprint or hashlib.sha256(encode([expected, after, reason]).encode()).hexdigest()
    )
    if known:
        if known[0] != fingerprint:
            raise ValueError("Commit identity conflict")
        return True
    if set(after) != set(TABLES) or any(
        not isinstance(after[t], dict) or not before[t].keys() <= after[t].keys() for t in TABLES
    ):
        raise ValueError("State tables and keys cannot be deleted")
    for step_id, step in after["steps"].items():
        for field in ("request_archive", "request_draft_archive"):
            digest = step.get(field)
            if (
                digest
                and digest != before["steps"].get(step_id, {}).get(field)
                and not db.execute(
                    "SELECT 1 FROM request_archives WHERE user_id=? AND digest=?", (user_id, digest)
                ).fetchone()
            ):
                raise ValueError("Missing archived request")
    changes = {
        t: {k: v for k, v in after[t].items() if before[t].get(k) != v or k not in before[t]}
        for t in TABLES
    }
    changes = {t: v for t, v in changes.items() if v}
    if changes:
        version = expected + 1
        append_journal(
            db,
            user_id,
            "runtime.commit",
            {"version": version, "reason": reason, "changes": changes},
            f"runtime:{version}",
        )
        db.execute(
            "UPDATE users SET version=?,state_json=? WHERE user_id=?",
            (version, encode(after), user_id),
        )
        project(db, user_id, before, after, changes)
    db.execute("INSERT INTO commits VALUES(?,?,?)", (user_id, commit_id, fingerprint))
    if patch_fingerprint:
        return {
            "version": expected + bool(changes),
            "archived_requests": {key: after["steps"][key]["request_archive"] for key in archiving},
            "archived_drafts": {
                key: after["steps"][key]["request_draft_archive"] for key in archiving_drafts
            },
            "replayed": False,
        }
    return True


def project(db, user_id, before, after, changes):
    pause = after["meta"].get("tool_wake_pause")
    if pause and pause != before["meta"].get("tool_wake_pause"):
        outbox.append(db, user_id, "agent.paused", pause, f"tool-budget:{pause['epoch']}:paused")
    elif before["meta"].get("tool_wake_pause") and not pause:
        outbox.append(
            db,
            user_id,
            "agent.resumed",
            {"reason": "external_input"},
            f"tool-budget:{after['meta'].get('tool_budget_epoch', 0)}:resumed",
        )
    for step_id, step in changes.get("steps", {}).items():
        db.execute(
            "INSERT INTO steps VALUES(?,?,?,?,?) ON CONFLICT(user_id,step_id) DO UPDATE SET "
            "status=excluded.status,body_json=excluded.body_json",
            (user_id, step_id, step["run_id"], step["status"], encode(step)),
        )
        old = before["steps"].get(step_id, {})
        if step.get("response") is not None and old.get("response") is None:
            outbox.append(
                db,
                user_id,
                "assistant.message",
                {**step["response"], "step_id": step_id},
                f"step:{step_id}:response",
            )
        if old.get("status") != step["status"] or old.get("error") != step.get("error"):
            outbox.append(
                db,
                user_id,
                "step.status",
                {
                    "step_id": step_id,
                    "status": step["status"],
                    "error": step.get("error"),
                    "reason": step.get("pause_reason"),
                    "trace_id": step["trace_id"],
                    "run_id": step["run_id"],
                },
                f"step:{step_id}:status:{after['meta'].get('retry_seq', 0)}:"
                f"{step.get('attempts', 0)}:{step['status']}",
            )
    for key, entry in changes.get("inputs", {}).items():
        db.execute("DELETE FROM inbox_events WHERE user_id=? AND identity=?", (user_id, key))
        if (
            key not in before["inputs"]
            and entry["queue"] == "user"
            and entry["item"]["source"] == "user.message"
        ):
            outbox.append(db, user_id, "user.message", entry["item"]["payload"], f"input:{key}")
    for batch_id, batch in changes.get("batches", {}).items():
        db.execute(
            "INSERT INTO tool_batches VALUES(?,?,?) ON CONFLICT(user_id,batch_id) "
            "DO UPDATE SET body_json=excluded.body_json",
            (user_id, batch_id, encode(batch)),
        )
    for key, call in changes.get("calls", {}).items():
        db.execute(
            "INSERT INTO tool_invocations VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(user_id,invocation_id) "
            "DO UPDATE SET status=excluded.status,deadline=excluded.deadline,"
            "body_json=excluded.body_json",
            (user_id, key, call["batch_id"], call["status"], call.get("deadline"), encode(call)),
        )
        old = before["calls"].get(key, {})
        if call["status"] == "waiting_approval" and old.get("status") != "waiting_approval":
            approval = call.get("approval") if isinstance(call.get("approval"), dict) else {}
            outbox.append(
                db,
                user_id,
                "permission.requested",
                {
                    **approval,
                    "invocation_id": key,
                    "batch_id": call["batch_id"],
                    "tool": call["tool"],
                    "arguments": call["arguments"],
                    "expires_at": call["approval_deadline"],
                },
                f"permission:{key}",
            )
        if call.get("permission") in {"approved", "denied", "expired"} and call.get(
            "permission"
        ) != old.get("permission"):
            permission_decision = (
                call.get("permission_decision")
                if isinstance(call.get("permission_decision"), dict)
                else {}
            )
            outbox.append(
                db,
                user_id,
                "permission.resolved",
                {
                    **permission_decision,
                    "invocation_id": key,
                    "status": call["permission"],
                },
                f"permission:{key}:resolved",
            )
        if call.get("terminal") and not old.get("terminal"):
            payload = call["terminal"]
            item = Inbound(
                "tool", "tool.terminal", payload, payload["event_id"], call.get("finished_dt") or ""
            )
            if not call.get("inline"):
                enqueue(db, user_id, item.to_dict())
            outbox.append(db, user_id, "tool.terminal", payload, payload["event_id"])

    from core.subagents.storage import project as project_subagent

    project_subagent(db, user_id, changes, after)
