"""Agent Team communication and dynamic workflow dependency execution."""

import json

import pytest

from core.subagents.storage import finish
from tests.test_tasks_e2e import specification


def create_mode(service, mode, key):
    args = specification("agent")
    args["intent_key"] = key
    args["executor"]["mode"] = mode
    return service.call(
        "task.create", user_id="u1", request_id="create_" + key, **args
    )["task"]


def running_actor(service, task_id):
    service.call("task.tick")
    detail = service.call("task.get", user_id="u1", task_id=task_id, view="detail")
    return "u1:" + detail["run"]["agent_id"], detail["run"]["run_id"]


def spawn(service, actor, key):
    return service.call(
        "subagent.spawn",
        user_id=actor,
        request_id=key,
        title=key,
        objective="完成一个清晰边界的研究任务",
        success_criteria="返回可核验的结论",
    )


def test_agent_team_persists_peer_messages_and_delivers_them(service):
    task = create_mode(service, "agent_team", "team")
    actor, run_id = running_actor(service, task["task_id"])
    first = spawn(service, actor, "alpha")
    second = spawn(service, actor, "beta")
    first_context = "u1:" + first["agent_id"]

    members = service.call("team.members", user_id=first_context)
    assert {item["role"] for item in members} == {"leader", "teammate"}
    message = service.call(
        "team.send",
        user_id=first_context,
        request_id="peer-1",
        recipient_agent_id=second["agent_id"],
        content="我已完成资料清洗，请核对引用。",
    )
    assert message["sender_agent_id"] == first["agent_id"]
    assert message["recipient_agent_id"] == second["agent_id"]
    assert service.call(
        "team.send",
        user_id=first_context,
        request_id="peer-1",
        recipient_agent_id=second["agent_id"],
        content="我已完成资料清洗，请核对引用。",
    ) == message
    second_context = "u1:" + second["agent_id"]
    inbox = service.call("team.inbox", user_id=second_context)
    assert inbox[0]["content"] == "我已完成资料清洗，请核对引用。"
    assert any(
        item["source"] == "app.task.team.message"
        for item in service.call("buffer.peek", user_id=second_context, kind="system")
    )
    detail = service.call(
        "task.get", user_id="u1", task_id=task["task_id"], view="detail"
    )
    assert detail["execution"]["mode"] == "agent_team"
    assert len(detail["execution"]["team_messages"]) == 1
    assert detail["execution"]["task_agent"]["communications"][0]["content"].startswith(
        "我已完成"
    )
    with service.transaction() as db:
        roles = {
            row[0]
            for row in db.execute(
                "SELECT role FROM task_delegations WHERE run_id=?", (run_id,)
            )
        }
    assert roles == {"teammate"}
    broadcast = service.call(
        "team.send",
        user_id=actor,
        request_id="broadcast",
        content="十分钟后统一汇总。",
    )
    assert {
        item["recipient_agent_id"] for item in broadcast["messages"]
    } == {first["agent_id"], second["agent_id"]}
    assert (
        service.call(
            "team.send",
            user_id=actor,
            request_id="broadcast",
            content="十分钟后统一汇总。",
        )
        == broadcast
    )


def test_dynamic_workflow_runs_only_ready_dag_nodes_and_joins_before_completion(service):
    task = create_mode(service, "dynamic_workflow", "workflow")
    actor, _ = running_actor(service, task["task_id"])
    result = service.call(
        "workflow.define",
        user_id=actor,
        request_id="define",
        nodes=[
            {
                "id": "collect",
                "title": "收集资料",
                "objective": "收集官方来源",
                "success_criteria": "至少有两条来源",
            },
            {
                "id": "compare",
                "title": "比较方案",
                "objective": "比较两种方案",
                "success_criteria": "列出取舍",
            },
            {
                "id": "review",
                "title": "交叉复核",
                "objective": "复核两份资料",
                "success_criteria": "结论一致",
                "depends_on": ["collect", "compare"],
            },
            {
                "id": "report",
                "title": "汇总报告",
                "objective": "整合最终结果",
                "success_criteria": "形成报告",
                "depends_on": ["review"],
            },
        ],
    )
    states = {node["id"]: node["status"] for node in result["nodes"]}
    assert states == {
        "collect": "running",
        "compare": "running",
        "review": "pending",
        "report": "pending",
    }
    detail = service.call(
        "task.get", user_id="u1", task_id=task["task_id"], view="detail"
    )
    assert detail["run"]["status"] == "waiting"
    assert detail["execution"]["mode"] == "dynamic_workflow"
    assert len(detail["execution"]["subagents"]) == 4
    assert {edge["to"] for edge in detail["execution"]["workflow"]["edges"]} == {
        "review",
        "report",
    }
    assert all(
        child["delegation_role"] == "workflow_node"
        for child in detail["execution"]["subagents"]
    )

    children = {
        node["id"]: node["agent_id"]
        for node in detail["execution"]["workflow"]["nodes"]
    }
    with service.transaction() as db:
        for node_id in ("collect", "compare"):
            row = db.execute(
                "SELECT * FROM subagents WHERE agent_id=?", (children[node_id],)
            ).fetchone()
            finish(db, row, "succeeded", node_id + " completed")
    service.call("task.tick")
    detail = service.call(
        "task.get", user_id="u1", task_id=task["task_id"], view="detail"
    )
    states = {
        node["id"]: node["status"]
        for node in detail["execution"]["workflow"]["nodes"]
    }
    assert states["collect"] == states["compare"] == "succeeded"
    assert states["review"] == "running"
    assert states["report"] == "pending"

    with service.transaction() as db:
        row = db.execute(
            "SELECT * FROM subagents WHERE agent_id=?", (children["review"],)
        ).fetchone()
        finish(db, row, "succeeded", "review completed")
    service.call("task.tick")
    with service.transaction() as db:
        row = db.execute(
            "SELECT * FROM subagents WHERE agent_id=?", (children["report"],)
        ).fetchone()
        finish(db, row, "succeeded", "report completed")
    service.call("task.tick")
    queued = service.call(
        "task.get", user_id="u1", task_id=task["task_id"], view="detail"
    )
    assert queued["run"]["status"] == "queued"
    service.call("task.tick")
    running = service.call(
        "task.get", user_id="u1", task_id=task["task_id"], view="detail"
    )
    assert running["run"]["status"] == "running"
    continuation = service.call("buffer.peek", user_id=actor, kind="user")
    assert any("report completed" in json.dumps(item, ensure_ascii=False) for item in continuation)
    completed = service.call(
        "task.report",
        user_id=actor,
        request_id="complete",
        decision="complete",
        result={
            "summary": "工作流完成并已汇总",
            "refs": [],
            "evidence": [
                {
                    "kind": "external",
                    "ref": "workflow",
                    "summary": "四个节点均成功",
                }
            ],
        },
    )
    assert completed["status"] == "succeeded"
    final = service.call(
        "task.get", user_id="u1", task_id=task["task_id"], view="detail"
    )
    assert final["task"]["lifecycle"] == "closed"
    assert all(
        node["status"] == "succeeded"
        for node in final["execution"]["workflow"]["nodes"]
    )


def test_dynamic_workflow_rejects_cycles_before_spawning_agents(service):
    task = create_mode(service, "dynamic_workflow", "cycle")
    actor, _ = running_actor(service, task["task_id"])
    with pytest.raises(ValueError, match="DAG without cycles"):
        service.call(
            "workflow.define",
            user_id=actor,
            request_id="cycle",
            nodes=[
                {
                    "id": "a",
                    "title": "A",
                    "objective": "A",
                    "success_criteria": "A",
                    "depends_on": ["b"],
                },
                {
                    "id": "b",
                    "title": "B",
                    "objective": "B",
                    "success_criteria": "B",
                    "depends_on": ["a"],
                },
            ],
        )
    assert len(service.call("subagent.list", user_id="u1")) == 1
    detail = service.call(
        "task.get", user_id="u1", task_id=task["task_id"], view="detail"
    )
    assert detail["execution"]["workflow"]["nodes"] == []


def test_execution_mode_guard_limits_team_and_workflow_tools(service):
    parallel = create_mode(service, "parallel_subagents", "guard_parallel")
    parallel_actor, _ = running_actor(service, parallel["task_id"])
    with pytest.raises(ValueError, match="FORBIDDEN"):
        service.call("task.guard", user_id=parallel_actor, tool="team_send")

    team = create_mode(service, "agent_team", "guard_team")
    team_actor, _ = running_actor(service, team["task_id"])
    assert service.call("task.guard", user_id=team_actor, tool="team_send")["role"] == (
        "task_agent"
    )
    teammate = spawn(service, team_actor, "guarded_teammate")
    teammate_context = "u1:" + teammate["agent_id"]
    assert service.call("task.guard", user_id=teammate_context, tool="team_inbox")["role"] == (
        "subagent"
    )
    with pytest.raises(ValueError, match="FORBIDDEN"):
        service.call("task.guard", user_id=teammate_context, tool="workflow_define")

    workflow = create_mode(service, "dynamic_workflow", "guard_workflow")
    workflow_actor, _ = running_actor(service, workflow["task_id"])
    assert service.call(
        "task.guard", user_id=workflow_actor, tool="workflow_define"
    )["role"] == ("task_agent")
    with pytest.raises(ValueError, match="FORBIDDEN"):
        service.call("task.guard", user_id=workflow_actor, tool="team_send")


def test_dynamic_workflow_failure_blocks_dependents_and_needs_attention(service):
    task = create_mode(service, "dynamic_workflow", "workflow_failure")
    actor, _ = running_actor(service, task["task_id"])
    defined = service.call(
        "workflow.define",
        user_id=actor,
        request_id="define_failure",
        nodes=[
            {
                "id": "source",
                "title": "收集",
                "objective": "收集资料",
                "success_criteria": "有资料",
            },
            {
                "id": "report",
                "title": "报告",
                "objective": "写报告",
                "success_criteria": "报告完成",
                "depends_on": ["source"],
            },
        ],
    )
    assert defined["status"]["state"] == "running"
    assert service.call(
        "workflow.define",
        user_id=actor,
        request_id="define_failure",
        nodes=[
            {
                "id": "source",
                "title": "收集",
                "objective": "收集资料",
                "success_criteria": "有资料",
            },
            {
                "id": "report",
                "title": "报告",
                "objective": "写报告",
                "success_criteria": "报告完成",
                "depends_on": ["source"],
            },
        ],
    ) == defined
    source_agent = next(
        node["agent_id"] for node in defined["nodes"] if node["id"] == "source"
    )
    with service.transaction() as db:
        row = db.execute(
            "SELECT * FROM subagents WHERE agent_id=?", (source_agent,)
        ).fetchone()
        finish(db, row, "failed", "source unavailable")
    service.call("task.tick")
    detail = service.call(
        "task.get", user_id="u1", task_id=task["task_id"], view="detail"
    )
    assert detail["run"]["status"] == "paused"
    assert detail["task"]["needs_attention"] is True
    states = {
        node["id"]: node["status"]
        for node in detail["execution"]["workflow"]["nodes"]
    }
    assert states == {"source": "failed", "report": "blocked"}
