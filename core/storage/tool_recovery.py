"""Replay a stored tool terminal without executing the tool or replacing its result."""

import json

from core.protocol.datatype.inbound import Inbound
from core.storage.runtime_transactions import enqueue


def replay(db, user_id, invocation_ids, *, dry_run=True):
    if (
        not isinstance(invocation_ids, list)
        or not 1 <= len(invocation_ids) <= 20
        or any(not isinstance(i, str) or len(i) > 256 for i in invocation_ids)
        or type(dry_run) is not bool
    ):
        raise ValueError("Expected 1..20 invocation IDs and a boolean dry_run")
    results = []
    for invocation_id in dict.fromkeys(invocation_ids):
        row = db.execute(
            "SELECT body_json FROM tool_invocations WHERE user_id=? AND invocation_id=?",
            (user_id, invocation_id),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown tool invocation")
        call = json.loads(row[0])
        terminal = call.get("terminal")
        if not terminal:
            results.append({"invocation_id": invocation_id, "status": "not_terminal"})
            continue
        item = Inbound(
            kind="tool",
            source="tool.terminal",
            source_id=terminal["event_id"],
            payload=terminal,
            occur_dt=call["finished_dt"],
        )
        claimed = db.execute(
            "SELECT 1 FROM users u,json_each(u.state_json,'$.inputs') i "
            "WHERE u.user_id=? AND i.key=? LIMIT 1",
            (user_id, item.identity),
        ).fetchone()
        pending = db.execute(
            "SELECT 1 FROM inbox_events WHERE user_id=? AND identity=?", (user_id, item.identity)
        ).fetchone()
        if claimed or pending:
            status = "already_claimed" if claimed else "already_buffered"
        else:
            status = "replayable" if dry_run else "replayed"
            if not dry_run:
                row_id = enqueue(db, user_id, item.to_dict())
                db.execute(
                    "UPDATE broker_outbox SET sent=0 WHERE id=? AND user_id=?", (row_id, user_id)
                )
        results.append({"invocation_id": invocation_id, "status": status})
    return {"results": results}
