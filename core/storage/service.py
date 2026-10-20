"""Storage RPC 的闭合集合；禁止客户端发送 SQL。"""

from __future__ import annotations

import json
import time

from core.protocol.datatype.inbound import Inbound
from core.storage import conversation_history, outbox, scheduling, status_summary
from core.storage import runtime_transactions as runtime
from core.storage.database import Database, encode


class StorageService:
    def __init__(self, database: Database):
        self.database = database

    def call(self, operation, **p):
        with self.database.transaction() as db:
            uid = p.get("user_id")
            if uid is not None:
                self.database.ensure(db, uid)
            fence = p.get("fence")
            if fence:
                scheduling.check(db, uid, fence)
            if operation == "state.summary":
                return status_summary.read(db, uid)
            if operation == "state.read":
                row = db.execute(
                    "SELECT version,state_json FROM users WHERE user_id=?", (uid,)
                ).fetchone()
                return {"version": row["version"], "state": json.loads(row["state_json"])}
            if operation == "state.commit":
                return runtime.commit(
                    db, uid, p["expected"], p["state"], p["reason"], p["commit_id"], fence
                )
            if operation == "users.list":
                return [r[0] for r in db.execute("SELECT user_id FROM users ORDER BY user_id")]
            if operation == "tools.users":
                return [
                    r[0]
                    for r in db.execute(
                        "SELECT DISTINCT user_id FROM tool_invocations WHERE status IN "
                        "('admitting','queued','running','waiting_approval')"
                    )
                ]
            if operation == "queue.add":
                return runtime.enqueue(db, uid, p["item"])
            if operation == "relay.claim":
                now = time.time()
                old = db.execute("SELECT * FROM relay_lease WHERE id=1").fetchone()
                if old and old["expires"] > now and old["owner"] != p["owner"]:
                    return None
                token = (
                    old["token"]
                    if old and old["owner"] == p["owner"]
                    else (old["token"] + 1 if old else 1)
                )
                db.execute(
                    "INSERT INTO relay_lease VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "owner=excluded.owner,token=excluded.token,expires=excluded.expires",
                    (p["owner"], token, now + p["ttl"]),
                )
                row = db.execute(
                    "SELECT * FROM broker_outbox WHERE sent=0 ORDER BY id LIMIT 1"
                ).fetchone()
                return {"token": token, "message": dict(row) if row else None}
            if operation == "relay.confirm":
                lease = db.execute("SELECT * FROM relay_lease WHERE id=1").fetchone()
                if (
                    not lease
                    or lease["owner"] != p["owner"]
                    or lease["token"] != p["token"]
                    or lease["expires"] <= time.time()
                ):
                    raise scheduling.LeaseLost("Publisher lease expired")
                db.execute("UPDATE broker_outbox SET sent=1 WHERE id=?", (p["id"],))
                return None
            if operation == "consumer.claim":
                return scheduling.claim_consumer(db, p["kind"], p["owner"], p["ttl"])
            if operation == "buffer.add":
                scheduling.check(db, uid, fence, required=True)
                item = Inbound.from_dict(p["item"])
                # Already claimed messages cannot re-enter the inbox on redelivery.
                state = json.loads(
                    db.execute("SELECT state_json FROM users WHERE user_id=?", (uid,)).fetchone()[0]
                )
                if item.identity not in state["inputs"]:
                    inserted = db.execute(
                        "INSERT OR IGNORE INTO inbox_events(user_id,kind,identity,item_json) "
                        "VALUES(?,?,?,?)",
                        (uid, item.kind, item.identity, encode(p["item"])),
                    ).rowcount
                    if inserted and item.kind == "user":
                        scheduling.wake(db, uid)
                return None
            if operation == "buffer.peek":
                return [
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT item_json FROM inbox_events WHERE user_id=? AND kind=? ORDER BY id",
                        (uid, p["kind"]),
                    )
                ]
            if operation == "buffer.remove":
                scheduling.check(db, uid, fence, required=True)
                state = json.loads(
                    db.execute("SELECT state_json FROM users WHERE user_id=?", (uid,)).fetchone()[0]
                )
                if p["identity"] not in state["inputs"]:
                    raise ValueError("Cannot remove an unclaimed input")
                db.execute(
                    "DELETE FROM inbox_events WHERE user_id=? AND identity=?", (uid, p["identity"])
                )
                return None
            if operation == "wake":
                if db.execute(
                    "SELECT 1 FROM inbox_events WHERE user_id=? LIMIT 1", (uid,)
                ).fetchone() or p.get("force"):
                    scheduling.wake(db, uid)
                return None
            if operation == "brain.claim":
                return scheduling.claim_brain(db, p["owner"], p["ttl"])
            if operation == "brain.finish":
                return scheduling.finish_brain(db, uid, fence, p.get("retry", False))
            if operation == "lease.acquire":
                if p["lane"] not in {
                    "compaction",
                    "consumer:user",
                    "consumer:tool",
                    "consumer:system",
                }:
                    raise ValueError("Invalid lease lane")
                return scheduling.acquire(db, uid, p["lane"], p["owner"], p["ttl"])
            if operation in {"lease.renew", "lease.release"}:
                scheduling.check(db, uid, fence, required=True)
                expires = time.time() + p["ttl"] if operation == "lease.renew" else 0
                db.execute(
                    "UPDATE leases SET expires=? WHERE user_id=? AND lane=?",
                    (expires, uid, fence["lane"]),
                )
                return None
            if operation == "supervisor.tick":
                now = time.time()
                rows = db.execute(
                    "SELECT u.user_id,u.next_invoke FROM users u WHERE next_invoke<=? "
                    "AND EXISTS(SELECT 1 FROM inbox_events i WHERE i.user_id=u.user_id) "
                    "ORDER BY next_invoke LIMIT 128",
                    (now,),
                ).fetchall()
                for row in rows:
                    runtime.enqueue(
                        db,
                        row["user_id"],
                        Inbound(
                            "system", "system.invoke", {}, f"invoke:{row['next_invoke']}"
                        ).to_dict(),
                    )
                    db.execute(
                        "UPDATE users SET next_invoke=? WHERE user_id=?",
                        (now + p["interval"], row["user_id"]),
                    )
                return len(rows)
            if operation == "outbox.append":
                return outbox.append(
                    db, uid, p["type"], p["payload"], p.get("key"), p.get("occur_dt")
                )
            if operation == "outbox.read":
                return outbox.read(db, uid, p["after"], p.get("through"), p.get("limit", 128))
            if operation == "history.page":
                return conversation_history.page(
                    db, uid, before=p.get("before"), limit=p.get("limit", 10)
                )
            if operation == "client.attach":
                attached = outbox.attach(db, uid, p["client_id"])
                if p.get("history_limit") is not None:
                    snapshot = conversation_history.page(db, uid, limit=p["history_limit"])
                    attached["delivery_cursor"] = attached["after_cursor"]
                    attached["after_cursor"] = snapshot["checkpoint"]
                    attached["snapshot"] = snapshot
                return attached
            if operation in {"client.ack", "client.delivered"}:
                return outbox.cursor(
                    db,
                    uid,
                    p["client_id"],
                    p["generation"],
                    p["seq"],
                    ack=operation == "client.ack",
                )
            if operation == "detail.put":
                db.execute(
                    "INSERT INTO result_details VALUES(?,?,?) ON CONFLICT(user_id,detail_id) "
                    "DO UPDATE SET content=excluded.content",
                    (uid, p["key"], p["content"]),
                )
                return None
            if operation == "detail.get":
                row = db.execute(
                    "SELECT content FROM result_details WHERE user_id=? AND detail_id=?",
                    (uid, p["key"]),
                ).fetchone()
                return row[0] if row else None
            if operation == "trace.append":
                span = p["span"]
                db.execute(
                    "INSERT INTO trace_spans(user_id,trace_id,span_id,body_json) VALUES(?,?,?,?)",
                    (uid, span["trace_id"], span["span_id"], encode(span)),
                )
                return None
            if operation == "trace.list":
                return [
                    r[0]
                    for r in db.execute(
                        "SELECT trace_id FROM trace_spans WHERE user_id=? "
                        "GROUP BY trace_id ORDER BY MAX(id) DESC",
                        (uid,),
                    )
                ]
            if operation == "trace.get":
                rows = db.execute(
                    "SELECT body_json FROM trace_spans WHERE user_id=? AND trace_id=? ORDER BY id",
                    (uid, p["trace_id"]),
                ).fetchall()
                latest = {s["span_id"]: s for s in (json.loads(r[0]) for r in rows)}
                return sorted(latest.values(), key=lambda s: s["start_unix_nano"])
            if operation == "journal.append":
                return runtime.append_journal(
                    db, uid, p["type"], p["payload"], p["key"], p.get("occur_dt")
                )
            if operation == "journal.read":
                return [
                    {"seq": r["seq"], "raw": json.loads(r["record_json"])}
                    for r in db.execute(
                        "SELECT seq,record_json FROM journal_events WHERE user_id=? AND seq>? "
                        "ORDER BY seq LIMIT ?",
                        (uid, p["after"], p.get("limit") or -1),
                    )
                ]
            if operation == "snapshot.put":
                return db.execute(
                    "INSERT INTO snapshots(user_id,body_json) VALUES(?,?)", (uid, encode(p["body"]))
                ).lastrowid
            if operation == "snapshot.get":
                row = db.execute(
                    "SELECT id,body_json FROM snapshots WHERE user_id=? ORDER BY id DESC LIMIT 1",
                    (uid,),
                ).fetchone()
                return {"id": row[0], "body": json.loads(row[1])} if row else None
            raise ValueError(f"Unknown storage operation: {operation}")
