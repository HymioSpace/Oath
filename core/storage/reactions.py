"""Atomic message feedback, output events and user-queue admission."""

import json

from core.storage import outbox
from core.storage.runtime_transactions import enqueue

EMOJI = {"👍", "❤️", "😂", "🎉", "🤔", "👎"}


def mutate(db, uid, data):
    actor, target, emoji, operation = (
        data.get(k) for k in ("actor", "target", "emoji", "operation_id")
    )
    if actor not in {"user", "assistant"} or not isinstance(target, str) or len(target) > 160:
        raise ValueError("Invalid reaction target")
    if emoji is not None and emoji not in EMOJI:
        raise ValueError("Invalid reaction")
    if not isinstance(operation, str) or not 1 <= len(operation) <= 128:
        raise ValueError("operation_id required")
    key = "reaction:" + actor + ":" + operation
    known = db.execute(
        "SELECT payload_json FROM outbox_events WHERE user_id=? AND idempotency_key=?", (uid, key)
    ).fetchone()
    if known:
        value = json.loads(known[0])
        if value.get("requested_target", value["target"]) != target or value["emoji"] != emoji:
            raise ValueError("Reaction operation conflict")
        return value
    requested_target = target
    role, _, identity = target.partition(":")
    if role != ("assistant" if actor == "user" else "user"):
        raise ValueError("Reactions must target the other speaker")
    if role == "user":
        row = db.execute(
            "SELECT seq,payload_json FROM outbox_events WHERE user_id=? AND type='user.message' "
            "AND (CAST(seq AS TEXT)=? OR "
            "json_extract(payload_json,'$.message_id')=?) ORDER BY seq LIMIT 1",
            (uid, identity, identity),
        ).fetchone()
        if row:
            target = "user:" + str(row["seq"])
    else:
        row = db.execute(
            "SELECT seq,payload_json FROM outbox_events WHERE user_id=? "
            "AND type='assistant.message' AND json_extract(payload_json,'$.step_id')=? LIMIT 1",
            (uid, identity),
        ).fetchone()
    if not row:
        raise ValueError("Message unavailable")
    payload = {
        "target": target,
        "actor": actor,
        "emoji": emoji,
        "requested_target": requested_target,
    }
    previous = db.execute(
        "SELECT e.payload_json FROM message_reactions r JOIN outbox_events e "
        "ON e.user_id=r.user_id AND e.seq=r.seq WHERE r.user_id=? AND r.target=? AND r.actor=?",
        (uid, target, actor),
    ).fetchone()
    changed = (json.loads(previous[0]).get("emoji") if previous else None) != emoji
    seq = outbox.append(db, uid, "message.reaction.changed", payload, key)
    db.execute(
        "INSERT OR REPLACE INTO message_reactions VALUES(?,?,?,?)", (uid, target, actor, seq)
    )
    if actor == "user" and changed:
        excerpt = str(json.loads(row["payload_json"]).get("content", ""))[:500]
        enqueue(
            db,
            uid,
            {
                "kind": "user",
                "source": "app.message.reaction",
                "source_id": key,
                "payload": {
                    **payload,
                    "content": "Message reaction feedback (not a new task): "
                    + json.dumps({**payload, "excerpt": excerpt}, ensure_ascii=False),
                },
            },
        )
    return payload
