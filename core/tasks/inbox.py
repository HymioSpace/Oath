"""Trusted task-to-agent messages, distinct from tool result receipts.

Only Storage transitions call this path. Public/broker tool inputs still require
an existing invocation and a matching terminal receipt.
"""

import json

from core.protocol.datatype.inbound import Inbound
from core.storage.database import encode
from core.storage.scheduling import wake


def deliver(db, context_id, source, payload, key):
    item = Inbound("system", "app.task." + source, payload, key)
    db.execute(
        "INSERT OR IGNORE INTO inbox_events(user_id,kind,identity,item_json) VALUES(?,?,?,?)",
        (context_id, "system", item.identity, encode(item.to_dict())),
    )
    wake(db, context_id)


def bind_pending(db, task, run, context_id):
    """Context sent before execution is consumed once by the next TaskAgent."""
    from core.tasks.storage import event

    rows = db.execute(
        "SELECT m.* FROM task_events m WHERE m.task_id=? AND m.type='message' "
        "AND (m.run_id=? OR m.run_id IS NULL) AND NOT EXISTS ("
        "SELECT 1 FROM task_events b WHERE b.task_id=m.task_id AND b.type='message_bound' "
        "AND json_extract(b.payload_json,'$.message_event_id')=m.event_id) ORDER BY m.event_id",
        (task["task_id"], run["run_id"]),
    ).fetchall()
    for message in rows:
        data = json.loads(message["payload_json"])
        deliver(
            db,
            context_id,
            "message",
            {"content": data["content"], "refs": data["refs"]},
            "task-message-event:" + str(message["event_id"]),
        )
        event(db, task, "message_bound", {"message_event_id": message["event_id"]}, run)
