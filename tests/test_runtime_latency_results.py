import json

import pytest

from core.journal.runtime_store import RuntimeStore
from core.protocol.datatype import User
from core.protocol.datatype.inbound import Inbound


def test_projected_tool_transition_keeps_conversation_and_checks_version(service):
    user = User("synthetic-projection")
    full = RuntimeStore(service, patch_commits=True)
    full.transact(
        user,
        "fixture",
        lambda s: s["conversation"].update(
            messages=[{"role": "user", "content": "large synthetic history" * 10000}]
        ),
    )
    projected = RuntimeStore(service, patch_commits=True, read_projection="tools")
    assert projected.read(user)["conversation"] == {}
    projected.transact(
        user, "fixture.tool", lambda s: s["meta"].update(tool=True), reuse_snapshot=True
    )
    full.transact(user, "fixture.concurrent", lambda s: s["meta"].update(other=True))
    projected.transact(
        user, "fixture.tool", lambda s: s["meta"].update(next=True), reuse_snapshot=True
    )
    state = full.read(user)
    assert len(state["conversation"]["messages"][0]["content"]) > 100000
    assert state["meta"] == {"tool": True, "other": True, "next": True}
    with pytest.raises(ValueError, match="cannot modify"):
        projected.transact(user, "fixture.invalid", lambda s: s["steps"].update(fake={}))


def test_selected_entries_handle_quoted_identities_and_scalar_types(service):
    user = User("synthetic-entries")
    store = RuntimeStore(service, patch_commits=True)
    key = '["tool","result:123"]'
    store.transact(
        user,
        "fixture",
        lambda s: s["meta"].update(
            {key: {"ok": True}, "flag": False, "text": "hello", "none": None}
        ),
    )
    assert store.entries(user, "meta", [key, "flag", "text", "none", "missing"]) == {
        key: {"ok": True},
        "flag": False,
        "text": "hello",
        "none": None,
    }


def test_result_replay_preserves_identity_and_never_reexecutes(service):
    user = "synthetic-replay"
    service.call("state.read", user_id=user)
    payload = {
        "event_id": "result:synthetic-0",
        "invocation_id": "synthetic-0",
        "batch_id": "synthetic",
        "outcome": "succeeded",
        "run_id": "old",
        "content": {"url": "/attachments/synthetic"},
    }
    call = {"terminal": payload, "finished_dt": "2026-01-01T00:00:00Z"}
    with service.transaction() as db:
        db.execute(
            "INSERT INTO tool_invocations VALUES(?,?,?,?,?,?)",
            (user, "synthetic-0", "synthetic", "succeeded", None, json.dumps(call)),
        )
    params = {"user_id": user, "invocation_ids": ["synthetic-0"]}
    assert service.call("tools.replay_results", **params)["results"][0]["status"] == "replayable"
    with service.transaction() as db:
        assert db.execute("SELECT count(*) FROM broker_outbox").fetchone()[0] == 0
    service.call("tools.replay_results", **params, dry_run=False)
    service.call("tools.replay_results", **params, dry_run=False)
    with service.transaction() as db:
        rows = db.execute("SELECT item_json FROM broker_outbox WHERE user_id=?", (user,)).fetchall()
        assert len(rows) == 1
        item = Inbound.from_dict(json.loads(rows[0][0]))
        assert item.payload == payload
    with pytest.raises(ValueError, match="Unknown"):
        service.call("tools.replay_results", user_id="different", invocation_ids=["synthetic-0"])
    RuntimeStore(service, patch_commits=True).transact(
        User(user),
        "fixture.claim",
        lambda s: s["inputs"].update(
            {item.identity: {"queue": "tool", "item": item.to_dict(), "phase": "done"}}
        ),
    )
    assert (
        service.call("tools.replay_results", **params, dry_run=False)["results"][0]["status"]
        == "already_claimed"
    )
