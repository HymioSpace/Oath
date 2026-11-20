"""Task integration flows through the real Storage boundary and durable SQLite."""

import pytest

from core.tasks.schema import validate


def specification(kind="program"):
    return {
        "intent_key": "daily_medication",
        "title": "Drink water",
        "objective": "Send reminder",
        "completion": "achieve",
        "acceptance": {
            "criteria": "Channel accepts notification",
        },
        "trigger": {"mode": "now"},
        "executor": (
            {"kind": "program", "action": "notify", "params": {"text": "Drink water"}}
            if kind == "program"
            else {"kind": "agent", "tool_profile": "research_readonly", "model_profile": "default"}
        ),
        "context": {"brief": "User requested reminder", "refs": []},
        "plan": [],
        "policy": {
            "run_budget": {
                "max_steps": 30,
                "max_tokens": 20000,
                "max_active_seconds": 900,
                "max_cost_microusd": None,
            },
            "task_budget": {
                "window": "lifetime",
                "timezone": "Asia/Shanghai",
                "limits": {
                    "max_steps": 100,
                    "max_tokens": 50000,
                    "max_active_seconds": 3600,
                    "max_cost_microusd": None,
                },
            },
            "notify": {"mode": "result", "destination_ref": "channel_current"},
            "overlap": "skip",
            "misfire": "skip",
            "grace_seconds": 900,
            "run_deadline_seconds": 7200,
            "max_retries": 2,
        },
    }


def create(service, kind="program"):
    return service.call("task.create", user_id="u1", request_id="create_1", **specification(kind))


def test_notification_flow_requires_receipt_and_does_not_spawn_model(service):
    made = create(service)
    validate("MutationResult", made)
    again = create(service)
    assert again["replayed"]
    assert again["task"]["task_id"] == made["task"]["task_id"]
    service.call("task.tick")
    delivery = service.call("task.delivery.claim", user_id="u1", owner="hymio")
    assert delivery["payload"] == {"text": "Drink water"}
    status = service.call("task.get", user_id="u1", task_id=made["task"]["task_id"], view="detail")
    validate("GetResult", status)
    assert status["run"]["status"] == "waiting"
    assert service.call("subagent.list", user_id="u1") == []
    service.call(
        "task.delivery.ack",
        user_id="u1",
        delivery_id=delivery["delivery_id"],
        owner="hymio",
        lease_token=delivery["lease_token"],
        status="accepted",
        receipt={"provider_id": "sent_1", "accepted_at": "2026-11-15T12:00:00Z"},
        error=None,
    )
    status = service.call("task.get", user_id="u1", task_id=made["task"]["task_id"], view="detail")
    validate("GetResult", status)
    assert status["run"]["status"] == "succeeded"
    assert status["task"]["lifecycle"] == "closed"
    assert service.call("task.delivery.claim", user_id="u1", owner="hymio") is None
    assert service.call("buffer.peek", user_id="u1", kind="system") == []
    for _ in range(3):
        service.call("supervisor.tick", interval=1)
    assert service.call("brain.claim", owner="should_not_run", ttl=30) is None
    with service.transaction() as db:
        assert db.execute("SELECT count(*) FROM steps").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM subagents").fetchone()[0] == 0


def test_owner_version_and_pause_controls(service):
    made = create(service)
    tid = made["task"]["task_id"]
    with pytest.raises(ValueError, match="NOT_FOUND"):
        service.call("task.get", user_id="other", task_id=tid, view="detail")
    with pytest.raises(ValueError, match="REVISION_CONFLICT"):
        service.call(
            "task.pause",
            user_id="u1",
            task_id=tid,
            expected_revision=2,
            reason="pause",
            request_id="pause_bad",
        )
    paused = service.call(
        "task.pause",
        user_id="u1",
        task_id=tid,
        expected_revision=1,
        reason="pause",
        request_id="pause_1",
    )
    service.call("task.tick")
    assert service.call("task.delivery.claim", user_id="u1", owner="hymio") is None
    service.call(
        "task.resume",
        user_id="u1",
        task_id=tid,
        expected_revision=paused["task"]["revision"],
        reason="resume",
        request_id="resume_1",
    )
    service.call("task.tick")
    assert service.call("task.delivery.claim", user_id="u1", owner="hymio") is not None


def test_agent_has_isolated_context_and_waits_for_input(service):
    made = create(service, "agent")
    service.call("task.tick")
    tid = made["task"]["task_id"]
    with service.transaction() as db:
        r = db.execute("SELECT * FROM task_runs WHERE task_id=?", (tid,)).fetchone()
        child = r["context_id"]
        rid = r["run_id"]
    assert child.startswith("u1:")
    report = service.call(
        "task.report",
        user_id=child,
        request_id="report_1",
        decision="wait",
        checkpoint={"progress": "Need a choice"},
        wait={"kind": "user", "reason": "Need input", "question": "Which source?"},
    )
    assert report["status"] == "waiting"
    waiting = service.call(
        "task.get", user_id="u1", task_id=tid, view="detail", include_execution=False
    )
    assert waiting["task"]["needs_attention"]
    assert waiting["run"]["wait_kind"] == "user"
    assert "execution" not in waiting
    with pytest.raises(ValueError, match="FORBIDDEN"):
        service.call("task.list", user_id=child)
    service.call(
        "task.message",
        user_id="u1",
        task_id=tid,
        expected_revision=1,
        reply_to=report["question_id"],
        content="Use public filings",
        refs=[],
        request_id="answer_1",
    )
    status = service.call("task.get", user_id="u1", task_id=tid, view="detail")
    assert status["run"]["run_id"] == rid
    assert status["task"]["needs_attention"] is False
    assert status["run"]["next_step"] != "Use public filings"
    assert status["run"]["status"] == "queued"
    service.call("task.tick")
    assert (
        service.call("task.get", user_id="u1", task_id=tid, view="detail")["run"]["status"]
        == "running"
    )


def test_completion_does_not_wait_for_notification(service):
    made = create(service, "agent")
    tid = made["task"]["task_id"]
    service.call("task.tick")
    with service.transaction() as db:
        r = db.execute("SELECT * FROM task_runs WHERE task_id=?", (tid,)).fetchone()
    result = {
        "summary": "Analysis with sources",
        "refs": ["filing_demo"],
        "evidence": [
            {"kind": "external", "ref": "filing_demo", "summary": "Reviewed public filing"}
        ],
    }
    service.call(
        "task.report",
        user_id=r["context_id"],
        request_id="done_1",
        decision="complete",
        checkpoint={"progress": "Analysis ready"},
        result=result,
    )
    value = service.call("task.get", user_id="u1", task_id=tid, view="detail")
    assert value["run"]["status"] == "succeeded"
    assert value["task"]["lifecycle"] == "closed"
    assert "stage" not in value["run"]
    assert value["run"]["delivery_status"] == "pending"
    validate("GetResult", value)
    with pytest.raises(ValueError, match="FORBIDDEN"):
        service.call(
            "task.review",
            user_id="u1",
            task_id=tid,
            expected_revision=1,
            run_id=r["run_id"],
            accepted=True,
            reason="obsolete",
            request_id="old_review",
        )
    d = service.call("task.delivery.claim", user_id="u1", owner="hymio")
    assert d["payload"]["text"] == result["summary"]
    service.call(
        "task.delivery.ack",
        user_id="u1",
        delivery_id=d["delivery_id"],
        owner="hymio",
        lease_token=d["lease_token"],
        status="accepted",
        receipt={"provider_id": "report_sent", "accepted_at": "2026-11-15T12:00:00Z"},
        error=None,
    )
    assert (
        service.call("task.get", user_id="u1", task_id=tid, view="detail")["run"]["status"]
        == "succeeded"
    )


def test_early_signal_resumes_same_execution(service):
    create(service, "agent")
    service.call("task.tick")
    with service.transaction() as db:
        r = db.execute("SELECT * FROM task_runs").fetchone()
    payload = {
        "source": "github.ci",
        "event_key": "done_47",
        "key": "pr_47",
        "payload": {"state": "success"},
        "occurred_at": "2026-11-15T12:00:00Z",
    }
    service.call("task.signal", user_id="u1", **payload)
    report = service.call(
        "task.report",
        user_id=r["context_id"],
        request_id="wait_ci",
        decision="wait",
        checkpoint={"progress": "CI pending"},
        wait={
            "kind": "event",
            "reason": "CI",
            "source": "github.ci",
            "key": "pr_47",
            "next_check_at": "2026-11-16T12:00:00Z",
        },
    )
    assert report["status"] == "queued"
    assert service.call("task.signal", user_id="u1", **payload)["signal_id"] == 1


def test_stale_epoch_cannot_execute_after_resume(service):
    made = create(service, "agent")
    tid = made["task"]["task_id"]
    service.call("task.tick")
    with service.transaction() as db:
        r = db.execute("SELECT * FROM task_runs").fetchone()
    binding = service.call("task.guard", user_id=r["context_id"], tool="task_report")
    service.call(
        "task.pause", user_id="u1", task_id=tid, expected_revision=1, reason="pause", request_id="p"
    )
    service.call(
        "task.resume",
        user_id="u1",
        task_id=tid,
        expected_revision=2,
        reason="resume",
        request_id="r",
    )
    service.call("task.tick")
    with pytest.raises(ValueError, match="LEASE_LOST"):
        service.call(
            "task.guard",
            user_id=r["context_id"],
            tool="task_report",
            expected_epoch=binding["control_epoch"],
        )


def test_http_task_flow_checks_auth_and_delivers_once(service):
    from starlette.testclient import TestClient

    from bootstrap.common.rpc import rpc_app
    from bootstrap.storage.process import OPERATIONS

    token = "local_test_" + "x" * 32
    app = rpc_app(service, token, OPERATIONS)
    with TestClient(app) as client:
        assert client.post("/v1/task.create", json={}).status_code == 401
        headers = {"Authorization": "Bearer " + token}
        response = client.post(
            "/v1/task.create",
            headers=headers,
            json={"user_id": "u1", "request_id": "http_create", **specification()},
        )
        assert response.status_code == 200, response.text
        tid = response.json()["result"]["task"]["task_id"]
        assert client.post("/v1/task.tick", headers=headers, json={}).status_code == 200
        d = client.post(
            "/v1/task.delivery.claim", headers=headers, json={"user_id": "u1", "owner": "hymio"}
        ).json()["result"]
        assert d["payload"]["text"] == "Drink water"
        assert (
            client.post(
                "/v1/task.delivery.ack",
                headers=headers,
                json={
                    "user_id": "u1",
                    "delivery_id": d["delivery_id"],
                    "owner": "hymio",
                    "lease_token": d["lease_token"],
                    "status": "accepted",
                    "receipt": {"provider_id": "http_sent", "accepted_at": "2026-11-15T12:00:00Z"},
                    "error": None,
                },
            ).status_code
            == 200
        )
        result = client.post(
            "/v1/task.get",
            headers=headers,
            json={"user_id": "u1", "task_id": tid, "view": "detail"},
        ).json()["result"]
        assert result["run"]["status"] == "succeeded"


def test_pause_pending_delivery_then_resume_completes(service):
    made = create(service)
    tid = made["task"]["task_id"]
    service.call("task.tick")
    service.call(
        "task.pause", user_id="u1", task_id=tid, expected_revision=1, reason="pause", request_id="p"
    )
    service.call(
        "task.resume",
        user_id="u1",
        task_id=tid,
        expected_revision=2,
        reason="resume",
        request_id="r",
    )
    assert service.call("task.delivery.claim", user_id="u1", owner="hymio") is not None


def test_report_replay_does_not_create_duplicate_questions(service):
    create(service, "agent")
    service.call("task.tick")
    with service.transaction() as db:
        r = db.execute("SELECT * FROM task_runs").fetchone()
    args = {
        "user_id": r["context_id"],
        "request_id": "same_report",
        "decision": "wait",
        "checkpoint": {"progress": "waiting"},
        "wait": {"kind": "user", "reason": "choice", "question": "Which source?"},
    }
    first = service.call("task.report", **args)
    assert service.call("task.report", **args) == first
    with service.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM task_events WHERE type='input_required'").fetchone()[0]
            == 1
        )


def test_waited_tool_completion_releases_original_run(service):
    create(service, "agent")
    service.call("task.tick")
    with service.transaction() as db:
        r = db.execute("SELECT * FROM task_runs").fetchone()
        db.execute(
            (
                "INSERT INTO tool_invocations(user_id,invocation_id,batch_id,"
                "status,body_json) VALUES(?,?,?,?,?)"
            ),
            (r["context_id"], "background_tool", "batch", "running", "{}"),
        )
    service.call(
        "task.report",
        user_id=r["context_id"],
        request_id="wait_tool",
        decision="wait",
        checkpoint={"progress": "Background process started"},
        wait={"kind": "tool", "reason": "Wait for process", "invocation_id": "background_tool"},
    )
    service.call("task.tick")
    with service.transaction() as db:
        assert db.execute("SELECT status FROM task_runs").fetchone()[0] == "waiting"
        db.execute("UPDATE tool_invocations SET status='succeeded'")
    service.call("task.tick")
    with service.transaction() as db:
        assert db.execute("SELECT status FROM task_runs").fetchone()[0] == "queued"


def test_capacity_does_not_roll_back_other_tasks(service):
    for n in range(10):
        args = specification("agent")
        args["intent_key"] = "parallel_" + str(n)
        service.call("task.create", user_id="u1", request_id="create_" + str(n), **args)
    service.call("task.tick")
    with service.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM task_runs WHERE status='running'").fetchone()[0] == 8
        )
        assert db.execute("SELECT count(*) FROM task_runs WHERE status='queued'").fetchone()[0] == 2


def test_recurring_task_remembers_result_and_schedules_another_run(service):
    import time

    args = specification()
    args["completion"] = "maintain"
    args["trigger"] = {"mode": "daily", "time": "20:00", "timezone": "Asia/Shanghai"}
    made = service.call("task.create", user_id="u1", request_id="daily", **args)
    tid = made["task"]["task_id"]
    for n in range(2):
        with service.transaction() as db:
            db.execute("UPDATE tasks SET next_due_at=? WHERE task_id=?", (time.time() - 1, tid))
        service.call("task.tick")
        d = service.call("task.delivery.claim", user_id="u1", owner="hymio")
        service.call(
            "task.delivery.ack",
            user_id="u1",
            delivery_id=d["delivery_id"],
            owner="hymio",
            lease_token=d["lease_token"],
            status="accepted",
            receipt={"provider_id": "daily_" + str(n), "accepted_at": "2026-11-15T12:00:00Z"},
            error=None,
        )
    detail = service.call("task.get", user_id="u1", task_id=tid, view="runs")
    assert detail["task"]["lifecycle"] == "active"
    assert len(detail["runs"]) == 2
    assert detail["runs"][0]["run_id"] != detail["runs"][1]["run_id"]
    assert detail["memory"]["summary"]


def test_parent_resume_preserves_independently_paused_child(service):
    parent = create(service)["task"]
    args = specification()
    args["intent_key"] = "child"
    args["parent_task_id"] = parent["task_id"]
    child = service.call("task.create", user_id="u1", request_id="child", **args)["task"]
    service.call(
        "task.pause",
        user_id="u1",
        task_id=child["task_id"],
        expected_revision=1,
        reason="Separate pause",
        request_id="child_pause",
    )
    service.call(
        "task.pause",
        user_id="u1",
        task_id=parent["task_id"],
        expected_revision=1,
        reason="Pause all",
        request_id="parent_pause",
    )
    service.call(
        "task.resume",
        user_id="u1",
        task_id=parent["task_id"],
        expected_revision=2,
        reason="Resume parent",
        request_id="parent_resume",
    )
    assert (
        service.call("task.get", user_id="u1", task_id=child["task_id"], view="detail")["task"][
            "lifecycle"
        ]
        == "paused"
    )


def test_task_model_step_budget_cannot_reset_through_continuation(service):
    args = specification("agent")
    args["policy"]["run_budget"]["max_steps"] = 2
    made = service.call("task.create", user_id="u1", request_id="budget", **args)
    service.call("task.tick")
    with service.transaction() as db:
        run = db.execute("SELECT * FROM task_runs").fetchone()
    for _n in range(2):
        fence = service.call("brain.claim", owner="test_worker", ttl=30)
        assert fence["user_id"] == run["context_id"]
        service.call("brain.finish", user_id=run["context_id"], fence=fence)
        service.call("wake", user_id=run["context_id"])
    assert service.call("brain.claim", owner="test_worker", ttl=30) is None
    assert (
        service.call("task.get", user_id="u1", task_id=made["task"]["task_id"], view="detail")[
            "run"
        ]["status"]
        == "paused"
    )
    detail = service.call(
        "task.get", user_id="u1", task_id=made["task"]["task_id"], view="detail"
    )
    assert detail["execution"]["task_agent"]["execution_status"] == "paused"
    assert detail["execution"]["task_agent"]["status"] == "paused"


def test_cancelling_budget_paused_task_settles_its_task_agent(service):
    made = create(service, "agent")
    task_id = made["task"]["task_id"]
    service.call("task.tick")
    with service.transaction() as db:
        run = db.execute("SELECT * FROM task_runs WHERE task_id=?", (task_id,)).fetchone()
        db.execute(
            "UPDATE task_runs SET status='paused',resume_status='running',"
            "error_json=? WHERE run_id=?",
            ('{"code":"BUDGET_EXHAUSTED"}', run["run_id"]),
        )
        db.execute(
            "UPDATE subagents SET status='paused',result='Paused: owning task run is paused' "
            "WHERE agent_id=?",
            (run["agent_id"],),
        )
    service.call(
        "task.cancel",
        user_id="u1",
        task_id=task_id,
        expected_revision=1,
        reason="User cancelled",
        request_id="cancel-paused",
    )
    with service.transaction() as db:
        run = db.execute("SELECT * FROM task_runs WHERE task_id=?", (task_id,)).fetchone()
        agent = db.execute(
            "SELECT status FROM subagents WHERE agent_id=?", (run["agent_id"],)
        ).fetchone()
    assert run["status"] == "cancelled"
    assert agent["status"] == "cancelled"


def test_budget_can_be_adjusted_to_resume_the_same_run(service):
    made = create(service, "agent")
    task_id = made["task"]["task_id"]
    service.call("task.tick")
    with service.transaction() as db:
        run = db.execute("SELECT * FROM task_runs WHERE task_id=?", (task_id,)).fetchone()
        db.execute(
            "UPDATE task_runs SET status='paused',resume_status='running',error_json=?,"
            "usage_json=? WHERE run_id=?",
            (
                '{"code":"BUDGET_EXHAUSTED"}',
                '{"steps":30,"tokens":20000,"active_seconds":900,"cost_microusd":0}',
                run["run_id"],
            ),
        )
        db.execute(
            "UPDATE subagents SET status='paused',result='Paused: run budget exhausted' "
            "WHERE agent_id=?",
            (run["agent_id"],),
        )
    adjusted = service.call(
        "task.adjust_budget",
        user_id="u1",
        task_id=task_id,
        expected_revision=1,
        reason="User increased the run budget",
        request_id="budget-adjust",
        run_budget={
            "max_steps": 60,
            "max_tokens": 50000,
            "max_active_seconds": 1800,
            "max_cost_microusd": None,
        },
    )
    assert adjusted["run"]["run_id"] == run["run_id"]
    assert adjusted["run"]["status"] == "queued"
    assert adjusted["run"]["error_code"] is None
    replay = service.call(
        "task.adjust_budget",
        user_id="u1",
        task_id=task_id,
        expected_revision=1,
        reason="User increased the run budget",
        request_id="budget-adjust",
        run_budget={
            "max_steps": 60,
            "max_tokens": 50000,
            "max_active_seconds": 1800,
            "max_cost_microusd": None,
        },
    )
    assert replay["replayed"] is True
    service.call("task.tick")
    resumed = service.call("task.get", user_id="u1", task_id=task_id, view="detail")
    assert resumed["run"]["status"] == "running"
    assert resumed["run"]["run_id"] == run["run_id"]


def test_no_progress_is_reported_once_without_model_polling(service):
    import time

    create(service, "agent")
    service.call("task.tick")
    with service.transaction() as db:
        db.execute(
            "UPDATE task_runs SET started_at=?,last_progress_at=?",
            (time.time() - 600, time.time() - 600),
        )
        db.execute("UPDATE scheduling SET ready_at=?", (time.time() - 600,))
    service.call("task.tick")
    service.call("task.tick")
    with service.transaction() as db:
        assert db.execute("SELECT status FROM task_runs").fetchone()[0] == "paused"
        assert (
            db.execute("SELECT count(*) FROM task_events WHERE type='blocked'").fetchone()[0] == 1
        )


def test_recent_model_activity_prevents_false_no_progress_pause(service):
    import time
    from datetime import UTC, datetime

    create(service, "agent")
    service.call("task.tick")
    with service.transaction() as db:
        run = db.execute("SELECT * FROM task_runs").fetchone()
        seq = db.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM conversation_events WHERE user_id=?",
            (run["context_id"],),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO conversation_events VALUES(?,?,?,?,?,?)",
            (
                run["context_id"],
                seq,
                "assistant.message",
                "recent-step",
                "{}",
                datetime.now(UTC).isoformat(),
            ),
        )
        db.execute(
            "UPDATE task_runs SET started_at=?,last_progress_at=?",
            (time.time() - 600, time.time() - 600),
        )
    service.call("task.tick")
    with service.transaction() as db:
        assert db.execute("SELECT status FROM task_runs").fetchone()[0] == "running"
        assert (
            db.execute("SELECT count(*) FROM task_events WHERE type='blocked'").fetchone()[0]
            == 0
        )


def test_ready_unclaimed_model_step_prevents_false_no_progress_pause(service):
    import time

    create(service, "agent")
    service.call("task.tick")
    with service.transaction() as db:
        run = db.execute("SELECT * FROM task_runs").fetchone()
        db.execute(
            "UPDATE task_runs SET started_at=?,last_progress_at=?",
            (time.time() - 600, time.time() - 600),
        )
        db.execute(
            "UPDATE scheduling SET requested=completed+1,ready_at=? WHERE user_id=?",
            (time.time(), run["context_id"]),
        )
    service.call("task.tick")
    with service.transaction() as db:
        assert db.execute("SELECT status FROM task_runs").fetchone()[0] == "running"


def test_pagination_cursor_is_bound_to_owner_and_query(service):
    for n in range(3):
        args = specification()
        args["intent_key"] = "page_" + str(n)
        service.call("task.create", user_id="u1", request_id="page_" + str(n), **args)
    first = service.call("task.list", user_id="u1", limit=2)
    second = service.call("task.list", user_id="u1", limit=2, cursor=first["next_cursor"])
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert first["items"][0]["task_id"] != second["items"][0]["task_id"]
    with pytest.raises(ValueError):
        service.call("task.list", user_id="other", limit=2, cursor=first["next_cursor"])
