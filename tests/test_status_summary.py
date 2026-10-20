import json

from tests._support.storage import Storage


def test_summary_is_small_and_user_isolated(tmp_path):
    storage = Storage(tmp_path / "state.sqlite3")
    storage.call("state.read", user_id="alice")
    storage.call("state.read", user_id="bob")
    with storage.transaction() as db:
        for user, status in [("alice", "completed"), ("bob", "running")]:
            db.execute(
                "INSERT INTO steps VALUES(?,?,?,?,?)",
                (user, "step", "run", status, json.dumps({"request": "x" * 2000000})),
            )
        db.execute(
            "INSERT INTO tool_invocations VALUES(?,?,?,?,?,?)",
            ("alice", "call", "batch", "waiting_approval", None, '{"tool":"bash"}'),
        )
    summary = storage.call("state.summary", user_id="alice")
    assert summary["busy"] is False
    assert summary["steps"]["step"]["status"] == "completed"
    assert summary["calls"]["call"]["status"] == "waiting_approval"
    assert len(json.dumps(summary)) < 2000
    assert storage.call("state.summary", user_id="bob")["busy"] is True
