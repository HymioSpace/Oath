import json

from bootstrap.storage.process import OPERATIONS
from core.subagents import storage as subagent_storage
from tests._support.storage import Storage


def test_storage_rpc_exposes_subagent_continue():
    assert "subagent.continue" in OPERATIONS


def test_completed_subagent_context_accepts_a_new_bounded_turn(service: Storage):
    child = service.call(
        "subagent.spawn",
        user_id="u1",
        request_id="kit-thread",
        title="应用编辑",
        objective="调整应用",
        success_criteria="页面和 API 保持一致",
        notify_parent=False,
        timeout_seconds=60,
    )
    child_user = f"u1:{child['agent_id']}"
    with service.transaction() as db:
        db.execute(
            "UPDATE subagents SET status='succeeded',result='first turn' WHERE agent_id=?",
            (child["agent_id"],),
        )

    resumed = service.call(
        "subagent.continue",
        user_id="u1",
        agent_id=child["agent_id"],
        message_id="turn-2",
        content="增加导出按钮，保持现有数据",
        success_criteria="导出可用，原记录不变",
        timeout_seconds=60,
    )

    assert resumed["status"] == "running"
    assert [turn["turn_id"] for turn in resumed["turns"]] == ["turn-2"]
    pending = service.call("buffer.peek", user_id=child_user, kind="user")
    assert [item["source"] for item in pending] == ["subagent.message"]
    assert pending[0]["payload"]["content"] == "增加导出按钮，保持现有数据"

    repeated = service.call(
        "subagent.continue",
        user_id="u1",
        agent_id=child["agent_id"],
        message_id="turn-2",
        content="增加导出按钮，保持现有数据",
        success_criteria="导出可用，原记录不变",
        timeout_seconds=60,
    )
    assert repeated["status"] == "running"
    assert len(service.call("buffer.peek", user_id=child_user, kind="user")) == 1


def test_private_subagent_context_does_not_notify_parent(service: Storage):
    child = service.call(
        "subagent.spawn",
        user_id="u1",
        request_id="private-kit-thread",
        title="应用编辑",
        objective="调整应用",
        success_criteria="构建通过",
        notify_parent=False,
        timeout_seconds=60,
    )
    service.call(
        "subagent.notify",
        user_id=f"u1:{child['agent_id']}",
        note_id="finding-1",
        content="已完成数据层检查",
    )

    assert service.call("buffer.peek", user_id="u1", kind="system") == []
    assert service.call(
        "subagent.get", user_id="u1", agent_id=child["agent_id"]
    )["notes"][0]["content"] == "已完成数据层检查"


def test_deferred_context_starts_only_after_binding(service: Storage):
    state = service.call("state.read", user_id="u1")["state"]
    state["meta"]["client_channel"] = "ios"
    with service.transaction() as db:
        db.execute(
            "UPDATE users SET state_json=? WHERE user_id='u1'",
            (json.dumps(state),),
        )
    child = service.call(
        "subagent.spawn",
        user_id="u1",
        request_id="deferred-kit-thread",
        title="应用开发",
        objective="构建应用",
        success_criteria="构建通过",
        notify_parent=True,
        defer_start=True,
        timeout_seconds=60,
    )
    child_user = f"u1:{child['agent_id']}"
    assert child["status"] == "waiting"
    assert service.call("buffer.peek", user_id=child_user, kind="tool") == []
    assert service.call("state.read", user_id=child_user)["state"]["meta"]["client_channel"] == (
        "ios"
    )

    started = service.call(
        "subagent.continue",
        user_id="u1",
        agent_id=child["agent_id"],
        message_id="start",
        content="开始构建",
        success_criteria="构建通过",
        timeout_seconds=60,
    )

    assert started["status"] == "running"
    assert service.call("buffer.peek", user_id=child_user, kind="user")[0]["source"] == (
        "subagent.message"
    )


def test_each_resumed_turn_can_finish_its_own_termination_commit(service: Storage):
    child = service.call(
        "subagent.spawn",
        user_id="u1",
        request_id="repeat-kit-thread",
        title="应用开发",
        objective="构建应用",
        success_criteria="完成",
        notify_parent=False,
        timeout_seconds=60,
    )
    with service.transaction() as db:
        row = subagent_storage.lookup(db, f"u1:{child['agent_id']}")
        subagent_storage.finish(db, row, "succeeded", "first")

    service.call(
        "subagent.continue",
        user_id="u1",
        agent_id=child["agent_id"],
        message_id="turn-two",
        content="继续修改",
        success_criteria="完成",
        timeout_seconds=60,
    )
    with service.transaction() as db:
        row = subagent_storage.lookup(db, f"u1:{child['agent_id']}")
        subagent_storage.finish(db, row, "succeeded", "second")

    assert service.call(
        "subagent.get", user_id="u1", agent_id=child["agent_id"]
    )["status"] == "succeeded"
