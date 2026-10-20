"""Transactional conversation read model; raw Outbox remains untouched."""

import json
import sqlite3
from typing import Any

from core.storage.database import encode

KINDS = frozenset(
    {
        "user.message",
        "assistant.message",
        "step.status",
        "tool.terminal",
        "permission.requested",
        "permission.resolved",
        "agent.paused",
        "agent.resumed",
    }
)


def apply(
    db: sqlite3.Connection,
    user_id: str,
    seq: int,
    kind: str,
    payload: dict[str, Any],
    created_at: str,
) -> None:
    step = payload.get("step_id", "")
    if kind == "assistant.delta" and payload.get("channel") == "content" and step:
        if db.execute(
            "SELECT 1 FROM conversation_events WHERE user_id=? "
            "AND type='assistant.message' AND entity=?",
            (user_id, step),
        ).fetchone():
            return
        attempt = int(payload.get("attempt", 0))
        old = db.execute(
            "SELECT * FROM conversation_streams WHERE user_id=? AND step_id=?", (user_id, step)
        ).fetchone()
        if old and old["attempt"] > attempt:
            return
        content = (old["content"] if old and old["attempt"] == attempt else "") + payload.get(
            "text", ""
        )
        db.execute(
            "INSERT OR REPLACE INTO conversation_streams VALUES(?,?,?,?,?,?)",
            (user_id, step, seq, attempt, content, old["created_at"] if old else created_at),
        )
    elif kind in KINDS:
        entity = (
            step
            if kind in {"assistant.message", "step.status"}
            else payload.get("invocation_id", "")
        )
        if kind == "assistant.message":
            db.execute(
                "DELETE FROM conversation_streams WHERE user_id=? AND step_id=?", (user_id, step)
            )
        if kind == "step.status":
            db.execute(
                "DELETE FROM conversation_events WHERE user_id=? AND type=? AND entity=?",
                (user_id, kind, entity),
            )
        db.execute(
            "INSERT OR REPLACE INTO conversation_events VALUES(?,?,?,?,?,?)",
            (user_id, seq, kind, entity, encode(payload), created_at),
        )


def update(
    db: sqlite3.Connection,
    user_id: str,
    seq: int,
    kind: str,
    payload: dict[str, Any],
    created_at: str,
) -> None:
    if db.execute("SELECT 1 FROM conversation_checkpoints WHERE user_id=?", (user_id,)).fetchone():
        apply(db, user_id, seq, kind, payload, created_at)
        db.execute("UPDATE conversation_checkpoints SET seq=? WHERE user_id=?", (seq, user_id))


def ensure(db, user_id):
    row = db.execute(
        "SELECT seq FROM conversation_checkpoints WHERE user_id=?", (user_id,)
    ).fetchone()
    after = row[0] if row else 0
    for event in db.execute(
        "SELECT * FROM outbox_events WHERE user_id=? AND seq>? ORDER BY seq", (user_id, after)
    ).fetchall():
        apply(
            db,
            user_id,
            event["seq"],
            event["type"],
            json.loads(event["payload_json"]),
            event["created_at"],
        )
        after = event["seq"]
    db.execute("INSERT OR REPLACE INTO conversation_checkpoints VALUES(?,?)", (user_id, after))
    return after


def event_of(user_id, row):
    return {
        "v": 2,
        "type": "event",
        "user_id": user_id,
        "event_seq": row["seq"],
        "event_type": row["type"],
        "payload": json.loads(row["payload_json"]),
        "occur_dt": row["created_at"],
    }


def page(db, user_id, *, before=None, limit=10):
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("limit must be 1..20 conversation turns")
    high = ensure(db, user_id)
    if before is not None and (type(before) is not int or not 1 <= before <= high + 1):
        raise ValueError("invalid history cursor")
    end = before if before is not None else high + 1
    turns = db.execute(
        "SELECT seq FROM conversation_events WHERE user_id=? "
        "AND type='user.message' AND seq<? ORDER BY seq DESC LIMIT ?",
        (user_id, end, limit),
    ).fetchall()
    start = turns[-1][0] if turns else 0
    # Include proactive messages before the earliest user turn on the final page.
    has_more = bool(
        db.execute(
            "SELECT 1 FROM conversation_events WHERE user_id=? AND seq<? LIMIT 1", (user_id, start)
        ).fetchone()
    )
    if len(turns) < limit:
        start, has_more = 0, False
    rows = db.execute(
        "SELECT * FROM conversation_events WHERE user_id=? AND seq>=? AND seq<? ORDER BY seq",
        (user_id, start, end),
    ).fetchall()
    events = {r["seq"]: event_of(user_id, r) for r in rows}
    streams = db.execute(
        "SELECT * FROM conversation_streams WHERE user_id=? AND seq>=? AND seq<?",
        (user_id, start, end),
    ).fetchall()
    batches = set()
    for row in rows:
        if row["type"] == "assistant.message":
            batches.add(row["entity"])
        elif row["type"] in {"tool.terminal", "permission.requested", "permission.resolved"}:
            found = db.execute(
                "SELECT batch_id FROM tool_invocations WHERE user_id=? AND invocation_id=?",
                (user_id, row["entity"]),
            ).fetchone()
            if found:
                batches.add(found[0])
    if before is None:
        batches.update(
            r[0]
            for r in db.execute(
                "SELECT DISTINCT batch_id FROM tool_invocations WHERE user_id=? "
                "AND status IN ('admitting','queued','running','waiting_approval')",
                (user_id,),
            )
        )
        batches.update(
            r[0]
            for r in db.execute(
                "SELECT step_id FROM steps WHERE user_id=? AND status IN ('prepared','running')",
                (user_id,),
            )
        )
    for batch in batches:
        ids = [
            r[0]
            for r in db.execute(
                "SELECT invocation_id FROM tool_invocations WHERE user_id=? AND batch_id=?",
                (user_id, batch),
            )
        ]
        for entity in [batch, *ids]:
            for row in db.execute(
                "SELECT * FROM conversation_events WHERE user_id=? AND entity=?", (user_id, entity)
            ):
                events[row["seq"]] = event_of(user_id, row)
        streams.extend(
            db.execute(
                "SELECT * FROM conversation_streams WHERE user_id=? AND step_id=? AND seq<?",
                (user_id, batch, start),
            ).fetchall()
        )
    for row in streams:
        events[row["seq"]] = {
            "v": 2,
            "type": "event",
            "user_id": user_id,
            "event_seq": row["seq"],
            "event_type": "assistant.delta",
            "occur_dt": row["created_at"],
            "payload": {
                "step_id": row["step_id"],
                "attempt": row["attempt"],
                "channel": "content",
                "text": row["content"],
            },
        }
    return {
        "events": sorted(events.values(), key=lambda e: e["event_seq"]),
        "checkpoint": high,
        "before_cursor": start if has_more else None,
        "has_more": has_more,
    }
