"""有期限的所有权；检查与状态写入使用同一 SQLite 事务。"""

import json
import time

from core.protocol.interface.storage import LeaseLost

__all__ = ["LeaseLost"]


def check(db, user_id, fence, *, required=False):
    if not fence:
        if required:
            raise LeaseLost("An execution lease is required")
        return
    row = db.execute(
        "SELECT * FROM leases WHERE user_id=? AND lane=?", (user_id, fence["lane"])
    ).fetchone()
    if (
        not row
        or row["owner"] != fence["owner"]
        or row["token"] != fence["token"]
        or row["expires"] <= time.time()
    ):
        raise LeaseLost("Lease expired or replaced")


def acquire(db, user_id, lane, owner, ttl):
    now = time.time()
    row = db.execute("SELECT * FROM leases WHERE user_id=? AND lane=?", (user_id, lane)).fetchone()
    if row and row["expires"] > now:
        return None
    token = row["token"] + 1 if row else 1
    db.execute(
        "INSERT INTO leases VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,lane) DO UPDATE SET "
        "owner=excluded.owner,token=excluded.token,expires=excluded.expires,touched=excluded.touched",
        (user_id, lane, owner, token, now + ttl, now),
    )
    return {"user_id": user_id, "lane": lane, "owner": owner, "token": token}


def wake(db, user_id, delay=0):
    from core.subagents.storage import lookup

    child = lookup(db, user_id)
    if child and child["status"] != "running":
        return
    state = json.loads(
        db.execute("SELECT state_json FROM users WHERE user_id=?", (user_id,)).fetchone()[0]
    )
    active = state["meta"].get("active_step")
    if (
        state["meta"].get("tool_wake_pause")
        and not db.execute(
            "SELECT 1 FROM inbox_events WHERE user_id=? AND kind IN ('user','system') LIMIT 1",
            (user_id,),
        ).fetchone()
    ):
        return
    if active and state["steps"][active]["status"] in {"failed", "paused"}:
        fresh = db.execute(
            "SELECT 1 FROM inbox_events WHERE user_id=? AND "
            "(kind='user' OR (kind='system' AND json_extract(item_json,'$.source') LIKE 'app.%')) "
            "LIMIT 1",
            (user_id,),
        ).fetchone()
        if not fresh:
            return
    db.execute(
        "UPDATE scheduling SET ready_at=CASE WHEN requested<=claimed THEN ? ELSE ready_at END, "
        "requested=MAX(requested,claimed+1) WHERE user_id=?",
        (time.time() + delay, user_id),
    )


def claim_brain(db, owner, ttl, *, allow_subagents=True):
    now = time.time()
    rows = db.execute(
        "SELECT s.user_id FROM scheduling s LEFT JOIN leases l "
        "ON l.user_id=s.user_id AND l.lane='brain' "
        "WHERE s.requested>s.completed AND s.ready_at<=? "
        "AND (l.expires IS NULL OR l.expires<=?) "
        "AND NOT EXISTS(SELECT 1 FROM subagents a WHERE a.storage_id=s.user_id "
        "AND (a.status!='running' OR a.deadline<=? OR ?=0)) "
        "ORDER BY s.ready_at LIMIT 1",
        (now, now, now, int(allow_subagents)),
    ).fetchall()
    for row in rows:
        user_id = row["user_id"]
        fence = acquire(db, user_id, "brain", owner, ttl)
        if fence:
            db.execute("UPDATE scheduling SET claimed=requested WHERE user_id=?", (user_id,))
            return fence
    return None


def finish_brain(db, user_id, fence, retry=False):
    check(db, user_id, fence, required=True)
    db.execute("UPDATE scheduling SET completed=claimed WHERE user_id=?", (user_id,))
    if retry:
        wake(db, user_id, delay=1)
    db.execute("UPDATE leases SET expires=0 WHERE user_id=? AND lane='brain'", (user_id,))


def claim_consumer(db, kind, owner, ttl):
    candidates = db.execute(
        "SELECT DISTINCT user_id FROM inbox_events WHERE kind=?", (kind,)
    ).fetchall()
    candidates = sorted(candidates, key=lambda row: _touched(db, row["user_id"], kind))
    for row in candidates:
        fence = acquire(db, row["user_id"], "consumer:" + kind, owner, ttl)
        if fence:
            return fence
    return None


def _touched(db, user_id, kind):
    row = db.execute(
        "SELECT touched FROM leases WHERE user_id=? AND lane=?", (user_id, "consumer:" + kind)
    ).fetchone()
    return row[0] if row else 0
