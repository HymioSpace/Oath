"""Cross-repository integration: Hymio UI/receipt bridge against real Oath Storage."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.test_tasks_e2e import specification


def test_hymio_reads_controls_and_delivers_oath_tasks(service, tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Hymio/backend"))
    pytest.importorskip("hymio_backend.tasks.remote")
    from hymio_backend.session_store import SessionStore
    from hymio_backend.tasks.remote import RemoteTasks, remote_task_routes

    token = tmp_path / "service_token"
    token.write_text("local-only-test-" + "x" * 32)
    settings = SimpleNamespace(
        database=tmp_path / "hymio.sqlite",
        service_token_file=token,
        origins=(),
        public_url="http://testserver",
        access_seconds=3600,
        refresh_seconds=3600,
    )
    sessions = SessionStore(settings)
    session = sessions.create_session({"iss": "test", "sub": "alice", "name": "Alice"})
    uid = session["user_id"]
    notifications = Mock()
    bridge = RemoteTasks(settings, sessions, notifications)

    async def oath(uid, path, data, request_id=None):
        if path.startswith("/tasks/"):
            return service.call(
                "task." + path.rsplit("/", 1)[1],
                user_id=uid,
                **({"request_id": request_id} if request_id else {}),
                **data,
            )
        return service.call("task.delivery." + path.rsplit("/", 1)[1], user_id=uid, **data)

    bridge.oath = oath

    async def body(request, **kwargs):
        return await request.json()

    app = Starlette(
        routes=remote_task_routes(
            settings,
            sessions,
            bridge,
            lambda r: r.headers.get("authorization", "").removeprefix("Bearer "),
            body,
        )
    )
    made = service.call("task.create", user_id=uid, request_id="create", **specification())
    tid = made["task"]["task_id"]
    headers = {"Authorization": "Bearer " + session["access_token"]}
    with TestClient(app) as client:
        assert client.get("/tasks").status_code == 401
        task = client.get("/tasks", headers=headers).json()["tasks"][0]
        assert task["id"] == tid
        assert client.post("/tasks", headers=headers, json={"action": "create"}).status_code == 400
        paused = client.post(
            "/tasks", headers=headers, json={"action": "pause", "task_id": tid}
        ).json()
        assert paused["state"] == "paused"
        client.post("/tasks", headers=headers, json={"action": "resume", "task_id": tid})
        service.call("task.tick")
        asyncio.run(bridge.deliver(uid))
        detail = client.post(
            "/tasks", headers=headers, json={"action": "get", "task_id": tid}
        ).json()
        assert detail["runs"][0]["status"] == "succeeded"
        assert detail["state"] == "closed"
        assert notifications.notify.call_count == 1
        asyncio.run(bridge.deliver(uid))
        assert notifications.notify.call_count == 1
        with sessions.transaction() as db:
            assert db.execute("SELECT count(*) FROM task_delivery_inbox").fetchone()[0] == 1

        # Crash after app inbox persistence, before Oath receives the receipt.

        args = specification()
        args["intent_key"] = "receipt_recovery"
        second = service.call("task.create", user_id=uid, request_id="create_recovery", **args)
        service.call("task.tick")

        async def lose_ack(uid, path, data, request_id=None):
            if path.endswith("/ack"):
                raise TimeoutError("Synthetic network interruption")
            return await oath(uid, path, data, request_id)

        bridge.oath = lose_ack
        with pytest.raises(TimeoutError):
            asyncio.run(bridge.deliver(uid))
        with service.transaction() as db:
            db.execute("UPDATE task_deliveries SET lease_until=0 WHERE status='inflight'")
        service.call("task.tick")
        restarted = RemoteTasks(settings, sessions, notifications)
        restarted.oath = oath
        asyncio.run(restarted.deliver(uid))
        recovered = service.call(
            "task.get", user_id=uid, task_id=second["task"]["task_id"], view="detail"
        )
        assert recovered["run"]["status"] == "succeeded"
        assert notifications.notify.call_count == 2

        # The HTTP detail preserves the real execution tree; list summaries stay small.
        bridge.oath = oath
        args = specification("agent")
        args["intent_key"] = "collaboration_graph"
        graph_task = service.call("task.create", user_id=uid, request_id="graph", **args)["task"]
        service.call("task.tick")
        graph = service.call("task.get", user_id=uid, task_id=graph_task["task_id"], view="detail")
        actor = uid + ":" + graph["run"]["agent_id"]
        child = service.call(
            "subagent.spawn",
            user_id=actor,
            request_id="graph_leaf",
            title="Fixture",
            objective="Check supplied fixture",
            success_criteria="Return fact",
        )
        response = client.post(
            "/tasks", headers=headers, json={"action": "get", "task_id": graph_task["task_id"]}
        ).json()
        assert response["execution"]["task_agent"]["agent_id"] == graph["run"]["agent_id"]
        assert response["execution"]["subagents"][0]["agent_id"] == child["agent_id"]
        summary = next(
            t
            for t in client.get("/tasks", headers=headers).json()["tasks"]
            if t["id"] == graph_task["task_id"]
        )
        assert summary["execution"] == {}
