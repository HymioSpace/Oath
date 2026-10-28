import pytest

from core.storage import outbox
from tests._support.storage import Storage


def test_activity_uses_owned_durable_events_and_excludes_tool_bodies(service: Storage):
    child = service.call(
        "subagent.spawn",
        user_id="u1",
        request_id="activity",
        title="Research",
        objective="Compare options",
        success_criteria="Report",
    )
    uid = "u1:" + child["agent_id"]
    with service.transaction() as db:
        outbox.append(
            db, uid, "step.status", {"step_id": "s1", "trace_id": "t1", "status": "running"}
        )
        outbox.append(db, uid, "assistant.message", {"step_id": "s1", "content": "Reading sources"})
        outbox.append(
            db,
            uid,
            "tool.terminal",
            {
                "tool": "browser",
                "status": "succeeded",
                "arguments": {"secret": "hidden"},
                "result": "private",
            },
        )
    result = service.call("subagent.activity", user_id="u1", agent_id=child["agent_id"])
    assert len(result["entries"]) == 3
    assert result["entries"][1]["trace_id"] == "t1"
    assert "private" not in str(result) and "hidden" not in str(result)
    assert (
        service.call(
            "subagent.activity",
            user_id="u1",
            agent_id=child["agent_id"],
            after=result["next_cursor"],
        )["entries"]
        == []
    )
    with pytest.raises(ValueError):
        service.call("subagent.activity", user_id="u2", agent_id=child["agent_id"])
