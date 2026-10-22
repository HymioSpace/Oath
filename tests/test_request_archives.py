import json
import sqlite3

from core.storage.database import encode
from core.storage.request_archives import compact, compact_existing, hydrate


def _state(status: str):
    return {
        "steps": {
            "step-1": {
                "status": status,
                "request": {"messages": [{"role": "user", "content": "你好呀"}]},
            }
        }
    }


def test_completed_request_moves_out_of_hot_state_and_stays_archived():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE request_archives("
        "user_id TEXT,digest TEXT,body BLOB,raw_bytes INTEGER,"
        "PRIMARY KEY(user_id,digest))"
    )
    state = _state("completed")

    assert compact(db, "user-1", state) is True
    step = state["steps"]["step-1"]
    assert step["request"] is None
    assert step["request_archive"]

    hydrate(db, "user-1", state)
    assert step["request"] is None

    hydrate(db, "user-1", state, include_completed=True)
    assert step["request"]["messages"][0]["content"] == "你好呀"


def test_active_request_stays_in_hot_state():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE request_archives("
        "user_id TEXT,digest TEXT,body BLOB,raw_bytes INTEGER,"
        "PRIMARY KEY(user_id,digest))"
    )
    state = _state("running")

    assert compact(db, "user-1", state) is False
    assert state["steps"]["step-1"]["request"] is not None


def test_recoverable_request_is_hydrated_from_archive():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE request_archives("
        "user_id TEXT,digest TEXT,body BLOB,raw_bytes INTEGER,"
        "PRIMARY KEY(user_id,digest))"
    )
    state = _state("completed")
    compact(db, "user-1", state)

    recoverable = _state("running")
    step = recoverable["steps"]["step-1"]
    step["request"] = None
    step["request_archive"] = state["steps"]["step-1"]["request_archive"]

    hydrate(db, "user-1", recoverable)
    assert step["request"]["messages"][0]["content"] == "你好呀"


def test_existing_compaction_updates_hot_state_and_derived_step():
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE request_archives("
        "user_id TEXT,digest TEXT,body BLOB,raw_bytes INTEGER,"
        "PRIMARY KEY(user_id,digest));"
        "CREATE TABLE users(user_id TEXT PRIMARY KEY,state_json TEXT);"
        "CREATE TABLE steps(user_id TEXT,step_id TEXT,body_json TEXT,"
        "PRIMARY KEY(user_id,step_id));"
    )
    state = _state("completed")
    db.execute("INSERT INTO users VALUES(?,?)", ("user-1", encode(state)))
    db.execute(
        "INSERT INTO steps VALUES(?,?,?)",
        ("user-1", "step-1", encode(state["steps"]["step-1"])),
    )

    compact_existing(db, "user-1")

    hot = json.loads(db.execute("SELECT state_json FROM users").fetchone()[0])
    derived = json.loads(db.execute("SELECT body_json FROM steps").fetchone()[0])
    assert hot["steps"]["step-1"]["request"] is None
    assert derived["request"] is None
    assert derived["request_archive"] == hot["steps"]["step-1"]["request_archive"]
