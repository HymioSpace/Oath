"""Completed requests live on disk; active requests remain in execution state."""

import hashlib
import json
import zlib

from core.storage.database import encode


def compact(db, user_id, state):
    changed = False
    active = state.get("meta", {}).get("active_step")
    for step_id, step in state["steps"].items():
        draft = step.get("request_draft")
        if (
            "active_step" in state.get("meta", {})
            and draft is not None
            and step_id != active
            and step.get("status") in {"paused", "failed", "completed"}
        ):
            raw = encode(draft).encode("utf-8")
            digest = hashlib.sha256(raw).hexdigest()
            db.execute(
                "INSERT OR IGNORE INTO request_archives(user_id,digest,body,raw_bytes) "
                "VALUES(?,?,?,?)",
                (user_id, digest, zlib.compress(raw, level=3), len(raw)),
            )
            step.update(request_draft=None, request_draft_archive=digest)
            changed = True
        elif step_id == active and draft is None and step.get("request_draft_archive"):
            row = db.execute(
                "SELECT body FROM request_archives WHERE user_id=? AND digest=?",
                (user_id, step["request_draft_archive"]),
            ).fetchone()
            if row is None:
                raise ValueError("Missing archived request draft")
            raw = zlib.decompress(row[0])
            if hashlib.sha256(raw).hexdigest() != step["request_draft_archive"]:
                raise ValueError("Archived draft checksum mismatch")
            step["request_draft"] = json.loads(raw)
            changed = True
        request = step.get("request")
        if step.get("status") != "completed":
            if request is None and step.get("request_archive"):
                hydrate(db, user_id, {"steps": {"active": step}})
            continue
        if request is None:
            continue
        raw = encode(request).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        db.execute(
            "INSERT OR IGNORE INTO request_archives(user_id,digest,body,raw_bytes) VALUES(?,?,?,?)",
            (user_id, digest, zlib.compress(raw, level=3), len(raw)),
        )
        step["request"] = None
        step["request_archive"] = digest
        changed = True
    return changed


def hydrate(db, user_id, state, *, include_completed=False):
    for step in state["steps"].values():
        draft_digest = step.get("request_draft_archive")
        if draft_digest and step.get("request_draft") is None:
            row = db.execute(
                "SELECT body FROM request_archives WHERE user_id=? AND digest=?",
                (user_id, draft_digest),
            ).fetchone()
            if row is None:
                raise ValueError("Missing archived request draft")
            raw = zlib.decompress(row[0])
            if hashlib.sha256(raw).hexdigest() != draft_digest:
                raise ValueError("Archived draft checksum mismatch")
            step["request_draft"] = json.loads(raw)
        digest = step.get("request_archive")
        if (
            not digest
            or step.get("request") is not None
            or (step.get("status") == "completed" and not include_completed)
        ):
            continue
        row = db.execute(
            "SELECT body FROM request_archives WHERE user_id=? AND digest=?", (user_id, digest)
        ).fetchone()
        if row is None:
            raise ValueError("Missing archived request")
        raw = zlib.decompress(row[0])
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("Archived request checksum mismatch")
        step["request"] = json.loads(raw)
    return state


def compact_existing(db, user_id):
    row = db.execute("SELECT state_json FROM users WHERE user_id=?", (user_id,)).fetchone()
    state = json.loads(row[0])
    if compact(db, user_id, state):
        db.execute("UPDATE users SET state_json=? WHERE user_id=?", (encode(state), user_id))
        for step_id, step in state["steps"].items():
            if step.get("request_archive") or step.get("request_draft_archive"):
                db.execute(
                    "UPDATE steps SET body_json=? WHERE user_id=? AND step_id=?",
                    (encode(step), user_id, step_id),
                )
