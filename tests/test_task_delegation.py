"""TaskAgent owns exactly one leaf layer, including restart, sleep and control."""

import json

import pytest

from core.journal.validation import parse_event_record
from core.protocol.datatype.user import User
from core.subagents.storage import finish
from tests._support.storage import Storage
from tests.test_tasks_e2e import create


def setup_task(service):
    task = create(service, "agent")["task"]
    service.call("task.tick")
    detail = service.call("task.get", user_id="u1", task_id=task["task_id"], view="detail")
    return task, "u1:" + detail["run"]["agent_id"], detail["run"]["run_id"]


def spawn(service, actor, request="leaf"):
    return service.call(
        "subagent.spawn",
        user_id=actor,
        request_id=request,
        title="Public filings",
        objective="Read supplied fixture",
        success_criteria="Return verified fact",
    )


def test_delegation_sleep_restart_join_and_scoped_graph(service, tmp_path):
    task, actor, run_id = setup_task(service)
    child = spawn(service, actor)
    leaf = "u1:" + child["agent_id"]
    assert service.call("task.context", user_id=actor)["role"] == "task_agent"
    assert service.call("task.context", user_id=leaf)["role"] == "subagent"
    assert spawn(service, actor)["agent_id"] == child["agent_id"]
    with pytest.raises(ValueError, match="leaf subagents"):
        spawn(service, leaf, "nested")
    outsider = spawn(service, "u1", "outside")
    with pytest.raises(ValueError, match="not owned"):
        service.call("subagent.get", user_id=actor, agent_id=outsider["agent_id"])
    with pytest.raises(ValueError, match="owned subagents"):
        service.call(
            "task.report",
            user_id=actor,
            request_id="early",
            decision="complete",
            checkpoint={"progress": "Not done"},
            result={
                "summary": "x",
                "refs": [],
                "evidence": [{"kind": "external", "ref": "fixture", "summary": "x"}],
            },
        )
    service.call(
        "task.report",
        user_id=actor,
        request_id="sleep",
        decision="wait",
        checkpoint={"progress": "Waiting for filings"},
        wait={"kind": "subagents", "reason": "Collect fact", "agent_ids": [child["agent_id"]]},
    )
    service._inner.database._owner.close()
    restored = Storage(tmp_path / "oath.sqlite3")
    restored.call("supervisor.tick", interval=1)
    detail = restored.call("task.get", user_id="u1", task_id=task["task_id"], view="detail")
    assert detail["run"]["status"] == "waiting"
    # Leaf retains execution rights while its parent sleeps, but cannot report for it.
    assert restored.call("task.guard", user_id=leaf, tool="task_fetch")["run_id"] == run_id
    with pytest.raises(ValueError, match="FORBIDDEN"):
        restored.call("task.guard", user_id=leaf, tool="task_report")
    with restored.transaction() as db:
        row = db.execute(
            "SELECT * FROM subagents WHERE agent_id=?", (child["agent_id"],)
        ).fetchone()
        finish(db, row, "succeeded", "Verified fixture fact")
    restored.call("supervisor.tick", interval=1)
    restored.call("supervisor.tick", interval=1)
    detail = restored.call("task.get", user_id="u1", task_id=task["task_id"], view="detail")
    assert detail["run"]["status"] == "running"
    assert detail["run"]["run_id"] == run_id
    assert detail["execution"]["task_agent"]["agent_id"] == actor.split(":")[1]
    assert detail["execution"]["subagents"][0]["result"] == "Verified fixture fact"
    with restored.transaction() as db:
        queue = db.execute(
            ("SELECT item_json FROM inbox_events WHERE user_id=?"), (actor,)
        ).fetchall()
    assert any("Verified fixture fact" in r[0] for r in queue)
    journal = restored.call("journal.read", user_id="u1", after=0)
    events = [e["raw"] for e in journal if e["raw"]["type"] == "task.event"]
    for entry in events:
        parse_event_record(json.dumps(entry), User("u1"))
    assert any(e["payload"]["kind"] == "subagent.completed" for e in events)
    assert any(e["payload"]["kind"] == "wait_satisfied" for e in events)
    assert any(
        e["payload"]["kind"] == "run_started" and e["payload"]["state"]["run_status"] == "running"
        for e in events
    )


def test_pause_resume_cancel_cascades_and_steering_is_durable(service, tmp_path):
    task, actor, _ = setup_task(service)
    child = spawn(service, actor)
    paused = service.call(
        "task.pause",
        user_id="u1",
        task_id=task["task_id"],
        expected_revision=1,
        reason="Hold",
        request_id="pause",
    )
    assert (
        service.call("subagent.get", user_id="u1", agent_id=child["agent_id"])["status"] == "paused"
    )
    service.call(
        "task.message",
        user_id="u1",
        task_id=task["task_id"],
        expected_revision=paused["task"]["revision"],
        request_id="steer",
        content="Use annual filings only",
        refs=[],
    )
    service._inner.database._owner.close()
    restored = Storage(tmp_path / "oath.sqlite3")
    inbox = restored.call("buffer.peek", user_id=actor, kind="system")
    assert any(
        i["source"] == "app.task.message" and "annual filings" in json.dumps(i) for i in inbox
    )
    resumed = restored.call(
        "task.resume",
        user_id="u1",
        task_id=task["task_id"],
        expected_revision=paused["task"]["revision"],
        reason="Go",
        request_id="resume",
    )
    restored.call("task.tick")
    assert (
        restored.call("subagent.get", user_id="u1", agent_id=child["agent_id"])["status"]
        == "running"
    )
    restored.call(
        "task.cancel",
        user_id="u1",
        task_id=task["task_id"],
        expected_revision=resumed["task"]["revision"],
        reason="Stop",
        request_id="cancel",
    )
    assert (
        restored.call("subagent.get", user_id="u1", agent_id=child["agent_id"])["status"]
        == "cancelled"
    )
    with pytest.raises(ValueError, match="INVALID_STATE"):
        restored.call("task.guard", user_id="u1:" + child["agent_id"], tool="task_fetch")


def test_running_steering_does_not_replace_execution(service):
    task, actor, run_id = setup_task(service)
    service.call(
        "task.message",
        user_id="u1",
        task_id=task["task_id"],
        expected_revision=1,
        request_id="steer_running",
        content="Compare two years",
        refs=[],
    )
    detail = service.call("task.get", user_id="u1", task_id=task["task_id"], view="detail")
    assert detail["run"]["run_id"] == run_id
    assert detail["run"]["status"] == "running"
    assert any(
        i["source"] == "app.task.message"
        for i in service.call("buffer.peek", user_id=actor, kind="system")
    )


def test_cancel_while_paused_closes_leaf_and_delegation(service):
    task, actor, _ = setup_task(service)
    child = spawn(service, actor)
    spawn(service, actor)
    with service.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM task_events WHERE type='subagent.created'").fetchone()[
                0
            ]
            == 1
        )
    paused = service.call(
        "task.pause",
        user_id="u1",
        task_id=task["task_id"],
        expected_revision=1,
        reason="Hold",
        request_id="pause",
    )
    service.call(
        "task.cancel",
        user_id="u1",
        task_id=task["task_id"],
        expected_revision=paused["task"]["revision"],
        reason="Stop",
        request_id="cancel",
    )
    assert (
        service.call("subagent.get", user_id="u1", agent_id=child["agent_id"])["status"]
        == "cancelled"
    )
    with service.transaction() as db:
        assert (
            db.execute(
                "SELECT state FROM task_delegations WHERE agent_id=?", (child["agent_id"],)
            ).fetchone()[0]
            == "closed"
        )


def test_message_before_agent_starts_is_bound_once(service):
    task = create(service, "agent")["task"]
    service.call(
        "task.message",
        user_id="u1",
        task_id=task["task_id"],
        expected_revision=1,
        request_id="before_start",
        content="Use the newly supplied annual report",
        refs=[],
    )
    service.call("task.tick")
    detail = service.call("task.get", user_id="u1", task_id=task["task_id"], view="detail")
    actor = "u1:" + detail["run"]["agent_id"]
    messages = service.call("buffer.peek", user_id=actor, kind="system")
    assert len([m for m in messages if m["source"] == "app.task.message"]) == 1
    service.call("task.tick")
    with service.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM task_events WHERE type='message_bound'").fetchone()[0]
            == 1
        )
