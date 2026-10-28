"""Client liveness without prompts, tool results, or accumulated runtime state."""

import json


def read(db, user_id):
    row = db.execute(
        "SELECT event_seq,json_extract(state_json,'$.meta.run_id') AS run_id, "
        "json_extract(state_json,'$.meta.tool_wake_pause') AS tool_wake_pause "
        "FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()
    step_counts = {
        r[0]: r[1]
        for r in db.execute(
            "SELECT status,count(*) FROM steps WHERE user_id=? GROUP BY status", (user_id,)
        )
    }
    call_counts = {
        r[0]: r[1]
        for r in db.execute(
            "SELECT status,count(*) FROM tool_invocations WHERE user_id=? GROUP BY status",
            (user_id,),
        )
    }
    fields = (
        "step_id,status,run_id,json_extract(body_json,'$.trace_id') AS trace_id,"
        "substr(json_extract(body_json,'$.error'),1,500) AS error"
    )
    steps = {
        r["step_id"]: dict(r)
        for r in db.execute(
            f"SELECT {fields} FROM steps WHERE user_id=? "
            "AND status IN ('prepared','running') ORDER BY rowid DESC LIMIT 200",
            (user_id,),
        )
    }
    latest = db.execute(
        f"SELECT {fields} FROM steps WHERE user_id=? ORDER BY rowid DESC LIMIT 1", (user_id,)
    ).fetchone()
    if latest:
        steps[latest["step_id"]] = dict(latest)
    calls = {
        r["invocation_id"]: dict(r)
        for r in db.execute(
            "SELECT invocation_id,batch_id,status,deadline,"
            "json_extract(body_json,'$.tool') AS tool "
            "FROM tool_invocations WHERE user_id=? "
            "AND status IN ('admitting','queued','running','waiting_approval') "
            "ORDER BY rowid DESC LIMIT 200",
            (user_id,),
        )
    }
    approvals = {}
    for approval_row in db.execute(
        "SELECT invocation_id,status,body_json FROM tool_invocations WHERE user_id=? "
        "AND (status='waiting_approval' "
        "OR json_extract(body_json,'$.permission_decision.decided_at') IS NOT NULL) "
        "ORDER BY rowid DESC LIMIT 200",
        (user_id,),
    ):
        body = json.loads(approval_row["body_json"])
        approval = body.get("approval") if isinstance(body.get("approval"), dict) else {}
        decision = (
            body.get("permission_decision")
            if isinstance(body.get("permission_decision"), dict)
            else None
        )
        approvals[approval_row["invocation_id"]] = {
            "invocation_id": approval_row["invocation_id"],
            "status": decision["status"] if decision else "pending",
            "tool": body.get("tool", ""),
            "expires_at": body.get("approval_deadline"),
            "approval": approval,
            "decision": decision,
        }
    return {
        "user_id": user_id,
        "event_seq": row["event_seq"],
        "meta": {
            "run_id": row["run_id"],
            "tool_wake_pause": json.loads(row["tool_wake_pause"])
            if row["tool_wake_pause"]
            else None,
        },
        "steps": steps,
        "calls": calls,
        "approvals": approvals,
        "busy": any(step_counts.get(k, 0) for k in ("prepared", "running"))
        or any(call_counts.get(k, 0) for k in ("admitting", "queued", "running")),
        "counts": {"steps": step_counts, "calls": call_counts},
        "buffer": {
            r[0]: r[1]
            for r in db.execute(
                "SELECT kind,count(*) FROM inbox_events WHERE user_id=? GROUP BY kind", (user_id,)
            )
        },
    }
