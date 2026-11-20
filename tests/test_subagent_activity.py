import pytest

from core.storage import outbox
from tests._support.storage import Storage


def test_activity_uses_owned_durable_events_and_excludes_tool_bodies(service: Storage):
    child = service.call(
        "subagent.spawn",
        user_id="u1",
        request_id="activity",
        title="Research",
        objective="Compare options\\n**Required**",
        success_criteria="Report",
    )
    assert child["objective"] == "Compare options\n**Required**"
    uid = "u1:" + child["agent_id"]
    long_content = "Reading sources\n" + ("source detail " * 400)
    with service.transaction() as db:
        outbox.append(
            db, uid, "step.status", {"step_id": "s1", "trace_id": "t1", "status": "running"}
        )
        outbox.append(
            db,
            uid,
            "assistant.message",
            {
                "step_id": "s1",
                "content": long_content,
                "description": "Inspect sources\\nThen compare",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "browser",
                        "arguments": {"description": "Inspect the source page"},
                    }
                ],
            },
        )
        outbox.append(
            db,
            uid,
            "tool.terminal",
            {
                "invocation_id": "s1-0",
                "status": "succeeded",
                "arguments": {"secret": "hidden"},
                "result": "private",
            },
        )
    result = service.call("subagent.activity", user_id="u1", agent_id=child["agent_id"])
    assert len(result["entries"]) == 3
    assert result["entries"][1]["trace_id"] == "t1"
    assert result["entries"][1]["created_at"] > 0
    assert result["entries"][1]["created_at_iso"].endswith("Z")
    assert "private" not in str(result) and "hidden" not in str(result)
    assert result["entries"][1]["tools"] == ["browser"]
    assert result["entries"][1]["tool_calls"][0]["description"] == "Inspect the source page"
    assert result["entries"][1]["content"] == long_content
    assert result["entries"][1]["content_truncated"] is False
    assert result["entries"][2]["tool"] == "browser"
    assert result["entries"][2]["tool_description"] == "Inspect the source page"
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
