"""对外事件先持久化，WS 只读；每个客户端独立确认。"""

import json
import sqlite3
import uuid
from typing import Any

from core.storage import conversation_history
from core.storage.database import encode
from core.utils.encoding import utc_now_iso


def append(
    db: sqlite3.Connection,
    user_id: str,
    type_: str,
    payload: dict[str, Any],
    key: str | None = None,
    occur_dt: str | None = None,
) -> int:
    key = key or uuid.uuid4().hex
    known = db.execute(
        "SELECT seq,type,payload_json FROM outbox_events WHERE user_id=? AND idempotency_key=?",
        (user_id, key),
    ).fetchone()
    body = encode(payload)
    if known:
        if known["type"] != type_ or known["payload_json"] != body:
            raise ValueError("Outbox idempotency conflict")
        return int(known["seq"])
    db.execute("UPDATE users SET event_seq=event_seq+1 WHERE user_id=?", (user_id,))
    seq = int(db.execute("SELECT event_seq FROM users WHERE user_id=?", (user_id,)).fetchone()[0])
    created_at = occur_dt or utc_now_iso()
    db.execute(
        "INSERT INTO outbox_events VALUES(?,?,?,?,?,?)",
        (user_id, seq, key, type_, body, created_at),
    )
    conversation_history.update(db, user_id, seq, type_, payload, created_at)
    return seq


def attach(db, user_id, client_id):
    if not isinstance(client_id, str) or not 1 <= len(client_id) <= 128:
        raise ValueError("client_id must be 1..128 characters")
    db.execute(
        "INSERT OR IGNORE INTO client_cursors(user_id,client_id) VALUES(?,?)", (user_id, client_id)
    )
    db.execute(
        "UPDATE client_cursors SET generation=generation+1,delivered=acked "
        "WHERE user_id=? AND client_id=?",
        (user_id, client_id),
    )
    row = db.execute(
        "SELECT * FROM client_cursors WHERE user_id=? AND client_id=?", (user_id, client_id)
    ).fetchone()
    high = db.execute("SELECT event_seq FROM users WHERE user_id=?", (user_id,)).fetchone()[0]
    return {"after_cursor": row["acked"], "through_cursor": high, "generation": row["generation"]}


def cursor(db, user_id, client_id, generation, seq, *, ack=False):
    row = db.execute(
        "SELECT * FROM client_cursors WHERE user_id=? AND client_id=?", (user_id, client_id)
    ).fetchone()
    if not row or row["generation"] != generation:
        raise ValueError("client_replaced")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise ValueError("invalid cursor")
    high = db.execute("SELECT event_seq FROM users WHERE user_id=?", (user_id,)).fetchone()[0]
    ceiling = row["delivered"] if ack else high
    if seq > ceiling:
        raise ValueError("cursor exceeds delivered events")
    field = "acked" if ack else "delivered"
    db.execute(
        f"UPDATE client_cursors SET {field}=MAX({field},?) WHERE user_id=? AND client_id=?",
        (seq, user_id, client_id),
    )


def read(db, user_id, after, through=None, limit=128):
    rows = db.execute(
        "SELECT * FROM outbox_events WHERE user_id=? AND seq>? AND seq<=? ORDER BY seq LIMIT ?",
        (
            user_id,
            after,
            through if through is not None else 9223372036854775807,
            min(max(int(limit), 1), 256),
        ),
    ).fetchall()
    return [
        {
            "v": 2,
            "type": "event",
            "user_id": user_id,
            "event_seq": r["seq"],
            "event_type": r["type"],
            "payload": json.loads(r["payload_json"]),
            "occur_dt": r["created_at"],
        }
        for r in rows
    ]
