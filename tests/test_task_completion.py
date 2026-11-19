"""Executor completion, notifications and genuine user waits are independent."""

import json
import time

import pytest

from core.tasks.schema import validate
from core.tasks.tools import tools
from tests._support.storage import Storage
from tests.test_tasks_e2e import create, specification


def done(service, task):
    service.call("task.tick")
    detail = service.call("task.get", user_id="u1", task_id=task["task_id"], view="detail")
    result = {"summary": "Computed result: 4", "refs": [], "evidence": []}
    reply = service.call(
        "task.report",
        user_id="u1:" + detail["run"]["agent_id"],
        request_id="complete",
        decision="complete",
        result=result,
    )
    assert reply["status"] == "succeeded"
    return service.call("task.get", user_id="u1", task_id=task["task_id"], view="detail")


def test_completion_needs_neither_invented_progress_nor_parent_review(service):
    task = create(service, "agent")["task"]
    detail = done(service, task)
    validate("GetResult", detail)
    assert detail["run"]["progress"] == ""
    assert detail["run"]["next_step"] == ""
    assert detail["run"]["status"] == "succeeded"
    assert detail["run"]["delivery_status"] == "pending"
    assert "stage" not in detail["run"]
    assert detail["execution"]["task_agent"]["result"] == "Computed result: 4"
    assert detail["execution"]["task_agent"]["status"] == "succeeded"
    assert detail["execution"]["task_agent"]["finished_at"] is not None
    assert "task_review" not in {t.definition.name for t in tools(service)}
    assert not any(e["type"] == "review_required" for e in detail["execution"]["events"])


def test_supervisor_settles_previously_completed_task_agent_once(service):
    task = create(service, "agent")["task"]
    detail = done(service, task)
    agent_id = detail["run"]["agent_id"]
    with service.transaction() as db:
        db.execute(
            "UPDATE subagents SET status='running',finished_at=NULL,result='' WHERE agent_id=?",
            (agent_id,),
        )
    service.call("supervisor.tick", interval=1)
    repaired = service.call("subagent.get", user_id="u1", agent_id=agent_id)
    assert repaired["status"] == "succeeded"
    assert repaired["result"] == detail["run"]["result"]["summary"]
    service.call("supervisor.tick", interval=1)
    assert service.call("subagent.get", user_id="u1", agent_id=agent_id) == repaired
    with service.transaction() as db:
        assert db.execute("SELECT count(*) FROM task_deliveries").fetchone()[0] == 1


def test_missing_notification_receipt_does_not_reopen_completed_task(service):
    task = create(service, "agent")["task"]
    detail = done(service, task)
    delivery = service.call("task.delivery.claim", user_id="u1", owner="sink")
    assert delivery is not None  # A closed, achieved task still owns its notification.
    with service.transaction() as db:
        db.execute("UPDATE task_deliveries SET lease_until=0")
    service.call("supervisor.tick", interval=1)
    unknown = service.call("task.get", user_id="u1", task_id=task["task_id"], view="detail")
    assert unknown["run"]["status"] == "succeeded"
    assert unknown["task"]["lifecycle"] == "closed"
    assert unknown["run"]["result"] == detail["run"]["result"]
    assert unknown["run"]["delivery_status"] == "unknown"
    service.call(
        "task.delivery.ack",
        user_id="u1",
        delivery_id=delivery["delivery_id"],
        owner="sink",
        lease_token=delivery["lease_token"],
        status="accepted",
        error=None,
        receipt={"provider_id": "persisted", "accepted_at": "2026-11-18T12:00:00Z"},
    )
    after = service.call("task.get", user_id="u1", task_id=task["task_id"], view="detail")
    assert after["run"]["version"] == detail["run"]["version"]
    assert after["run"]["delivery_status"] == "accepted"


def test_notification_can_be_disabled_without_blocking_completion(service):
    args = specification("agent")
    args["policy"]["notify"]["mode"] = "none"
    task = service.call("task.create", user_id="u1", request_id="silent", **args)["task"]
    detail = done(service, task)
    assert detail["run"]["delivery_status"] == "none"
    assert service.call("task.delivery.claim", user_id="u1", owner="sink") is None


def test_recurring_task_remains_active_after_one_successful_run(service):
    args = specification("agent")
    args.update(
        completion="maintain",
        trigger={"mode": "daily", "time": "21:00", "timezone": "Asia/Shanghai"},
    )
    task = service.call("task.create", user_id="u1", request_id="recurring", **args)["task"]
    with service.transaction() as db:
        db.execute("UPDATE tasks SET next_due_at=?", (time.time() - 1,))
    detail = done(service, task)
    assert detail["task"]["lifecycle"] == "active"
    assert detail["task"]["next_due_at"] is not None


@pytest.mark.parametrize("old_review", ["automatic", "user"])
def test_v3_pending_results_recover_without_reintroducing_review(service, tmp_path, old_review):
    task = create(service, "agent")["task"]
    service.call("task.tick")
    with service.transaction() as db:
        run = db.execute("SELECT * FROM task_runs").fetchone()
        snapshot = json.loads(run["spec_snapshot_json"])
        snapshot["acceptance"].update(review=old_review, require_delivery=True)
        wait = {
            "wait_id": "old_question",
            "condition": {"kind": "user", "reason": "Explicit confirmation", "question": "Accept?"},
            "signal_floor": 0,
            "next_check_at": None,
        }
        db.execute(
            "UPDATE task_runs SET status='waiting',stage='verify',"
            "spec_snapshot_json=?,wait_json=?,result_json=?",
            (
                json.dumps(snapshot),
                json.dumps(wait),
                json.dumps({"summary": "Previous result", "refs": [], "evidence": []}),
            ),
        )
        db.execute("UPDATE tasks SET acceptance_json=?", (json.dumps(snapshot["acceptance"]),))
        db.execute("UPDATE schema_version SET version=3")
    service._inner.database._owner.close()
    restored = Storage(tmp_path / "oath.sqlite3")
    restored.call("supervisor.tick", interval=1)
    detail = restored.call("task.get", user_id="u1", task_id=task["task_id"], view="detail")
    assert detail["spec"]["acceptance"] == {"criteria": "Channel accepts notification"}
    if old_review == "user":
        assert detail["run"]["status"] == "waiting"
        restored.call(
            "task.message",
            user_id="u1",
            task_id=task["task_id"],
            expected_revision=1,
            reply_to="old_question",
            content="Accepted",
            refs=[],
            request_id="answer",
        )
        restored.call("task.tick")
        detail = restored.call("task.get", user_id="u1", task_id=task["task_id"], view="detail")
        assert detail["run"]["status"] == "running"
    else:
        assert detail["run"]["status"] == "succeeded"
        restored.call("supervisor.tick", interval=1)
        with restored.transaction() as db:
            assert db.execute("SELECT count(*) FROM task_deliveries").fetchone()[0] == 1
